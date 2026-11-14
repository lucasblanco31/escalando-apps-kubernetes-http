from flask import Flask, render_template, Response, stream_with_context, request
from kubernetes import client, config
import psycopg2
import redis as redis_lib
import urllib.request
import urllib.parse
import json
import time
import threading
import random
import uuid
import os

app = Flask(__name__)

NAMESPACE   = os.getenv('NAMESPACE', 'keda-app')
DEPLOYMENTS = ['vote', 'worker']
INTERVAL    = int(os.getenv('INTERVAL', '2'))
DB_DSN      = os.getenv('DB_DSN', 'host=postgres user=postgres password=postgres')
REDIS_HOST         = os.getenv('REDIS_HOST', 'redis')
REDIS_PORT         = int(os.getenv('REDIS_SVC_PORT', '6379'))
INTERCEPTOR_ADMIN  = os.getenv('INTERCEPTOR_ADMIN', 'http://keda-add-ons-http-interceptor-admin.keda:9090')
VOTE_URL           = os.getenv('VOTE_URL', 'http://keda-add-ons-http-interceptor-proxy.keda:8080')
VOTE_HOST          = os.getenv('VOTE_HOST', '')  # Host que el interceptor usa para enrutar (el de la app); vacío = el de VOTE_URL
OPTION_A           = os.getenv('OPTION_A', 'Maradona')
OPTION_B           = os.getenv('OPTION_B', 'Messi')
DEMO_CODE          = os.getenv('DEMO_CODE', '')  # protege reinicio y generador de carga; viene de un Secret

def load_k8s():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

load_k8s()
v1      = client.CoreV1Api()
_redis  = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
_prev_req_count = None
_prev_redis_count = None


def get_pods():
    pods = v1.list_namespaced_pod(namespace=NAMESPACE)
    result = {d: [] for d in DEPLOYMENTS}

    for pod in pods.items:
        label = pod.metadata.labels.get('app', '') if pod.metadata.labels else ''
        if label not in DEPLOYMENTS:
            continue

        age_s = int(time.time() - pod.metadata.creation_timestamp.timestamp())
        if age_s < 60:
            age = f"{age_s}s"
        elif age_s < 3600:
            age = f"{age_s // 60}m"
        else:
            age = f"{age_s // 3600}h"

        ready, total, restarts = 0, len(pod.spec.containers), 0
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                if cs.ready:
                    ready += 1
                restarts += cs.restart_count

        result[label].append({
            'name':     pod.metadata.name,
            'status':   pod.status.phase or 'Unknown',
            'ready':    f"{ready}/{total}",
            'restarts': restarts,
            'age':      age,
        })

    return result


def get_nodes():
    try:
        nodes = []
        for n in v1.list_node().items:
            labels = n.metadata.labels or {}
            age_s = int(time.time() - n.metadata.creation_timestamp.timestamp())
            ready = any(c.type == 'Ready' and c.status == 'True' for c in (n.status.conditions or []))
            nodes.append({
                'name':     n.metadata.name,
                'type':     labels.get('node.kubernetes.io/instance-type', '?'),
                'capacity': labels.get('karpenter.sh/capacity-type', '?'),
                'pool':     labels.get('karpenter.sh/nodepool', '?'),
                'ready':    ready,
                'age':      f"{age_s}s" if age_s < 60 else (f"{age_s // 60}m" if age_s < 3600 else f"{age_s // 3600}h"),
            })
        return sorted(nodes, key=lambda x: x['name'])
    except Exception:
        return None


def get_redis_queue():
    try:
        return int(_redis.llen('votes'))
    except Exception:
        return None


_http_cache = {'ts': 0.0, 'value': (None, None)}
_http_lock  = threading.Lock()


def get_http_metric():
    # Con varias pestañas abiertas cada stream pediría la métrica y se repartirían
    # el delta entre ellas: se calcula una vez por intervalo y se comparte
    with _http_lock:
        now = time.time()
        if now - _http_cache['ts'] >= INTERVAL - 0.2:
            elapsed = now - _http_cache['ts'] if _http_cache['ts'] else INTERVAL
            _http_cache['value'] = _compute_http_metric(elapsed)
            _http_cache['ts'] = now
        return _http_cache['value']


def _compute_http_metric(elapsed):
    # Fuente 1: admin del interceptor de KEDA (lo que ve KEDA).
    # Fuente 2 (respaldo): contador que incrementa la app vote en Redis. Desde la
    # versión 0.14 del add-on el cálculo de req/s pasó al scaler y /queue puede no
    # traer RequestCount: en ese caso se usa el respaldo.
    global _prev_req_count, _prev_redis_count
    try:
        with urllib.request.urlopen(f'{INTERCEPTOR_ADMIN}/queue', timeout=2) as resp:
            data = json.loads(resp.read())
        if any(isinstance(v, dict) and 'RequestCount' in v for v in data.values()):
            total = sum(v.get('RequestCount', 0) for v in data.values() if isinstance(v, dict))
            if _prev_req_count is None:
                _prev_req_count = total
                return 0.0, 'interceptor'
            delta = max(0, total - _prev_req_count)
            _prev_req_count = total
            return round(delta / elapsed, 2), 'interceptor'
    except Exception:
        pass
    try:
        total = int(_redis.get('http_requests') or 0)
        if _prev_redis_count is None:
            _prev_redis_count = total
            return 0.0, 'pods'
        delta = max(0, total - _prev_redis_count)
        _prev_redis_count = total
        return round(delta / elapsed, 2), 'pods'
    except Exception:
        return None, None


def get_votes():
    try:
        conn = psycopg2.connect(DB_DSN, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT vote, COUNT(id) FROM votes GROUP BY vote")
            rows = {row[0]: int(row[1]) for row in cur.fetchall()}
        conn.close()
        a = rows.get('a', 0)
        b = rows.get('b', 0)
        total = a + b
        if total > 0:
            pct_a = round(a / total * 100)
            pct_b = 100 - pct_a
        else:
            pct_a = pct_b = 50
        return {'a': a, 'b': b, 'pct_a': pct_a, 'pct_b': pct_b, 'total': total}
    except Exception:
        return None


class LoadTest:
    def __init__(self):
        self._lock       = threading.Lock()
        self._stop_event = threading.Event()
        self.running     = False
        self.votes_a     = 0
        self.votes_b     = 0

    def start(self, ratio_a=50, concurrency=20):
        with self._lock:
            if self.running:
                return
            self.running = True
            self.votes_a = 0
            self.votes_b = 0
        self._stop_event.clear()

        def worker():
            while not self._stop_event.is_set():
                voter_id = uuid.uuid4().hex[:16]
                option   = 'a' if random.random() * 100 < ratio_a else 'b'
                try:
                    data = urllib.parse.urlencode({'vote': option}).encode()
                    req  = urllib.request.Request(
                        VOTE_URL, data=data,
                        headers={'Cookie': f'voter_id={voter_id}', **({'Host': VOTE_HOST} if VOTE_HOST else {})},
                        method='POST'
                    )
                    urllib.request.urlopen(req, timeout=10)
                    with self._lock:
                        if option == 'a':
                            self.votes_a += 1
                        else:
                            self.votes_b += 1
                except Exception:
                    pass
                # interruptible sleep: wakes immediately on stop()
                self._stop_event.wait(timeout=1)

        for _ in range(concurrency):
            threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self._stop_event.set()
        with self._lock:
            self.running = False

    def status(self):
        with self._lock:
            return {
                'running': self.running,
                'votes_a': self.votes_a,
                'votes_b': self.votes_b,
            }


_load_test = LoadTest()


def code_ok():
    # Sin DEMO_CODE configurado, nadie puede reiniciar ni generar carga desde el panel público
    return bool(DEMO_CODE) and request.headers.get('X-Demo-Code', '') == DEMO_CODE


@app.route('/healthz')
def healthz():
    return 'ok'


@app.route('/reset', methods=['POST'])
def reset():
    if not code_ok():
        return {'ok': False, 'error': 'código incorrecto'}, 403
    try:
        _redis.delete('votes', 'http_requests')
    except Exception:
        pass
    try:
        conn = psycopg2.connect(DB_DSN, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM votes")
        conn.close()
    except Exception as e:
        return {'ok': False, 'error': str(e)}, 500
    return {'ok': True}


@app.route('/load-test/start', methods=['POST'])
def load_test_start():
    if not code_ok():
        return {'ok': False, 'error': 'código incorrecto'}, 403
    body        = request.get_json(silent=True) or {}
    ratio_a     = int(body.get('ratio_a', 50))
    concurrency = max(1, min(int(body.get('concurrency', 20)), 150))  # ~1 req/s por hilo
    _load_test.start(ratio_a=ratio_a, concurrency=concurrency)
    return {'ok': True}


@app.route('/load-test/stop', methods=['POST'])
def load_test_stop():
    if not code_ok():
        return {'ok': False, 'error': 'código incorrecto'}, 403
    _load_test.stop()
    return {'ok': True}


@app.route('/')
def index():
    return render_template('index.html', deployments=DEPLOYMENTS, option_a=OPTION_A, option_b=OPTION_B)


@app.route('/stream')
def stream():
    def generate():
        while True:
            try:
                http_rate, http_source = get_http_metric()
                payload = {
                    'pods':      get_pods(),
                    'nodes':     get_nodes(),
                    'votes':     get_votes(),
                    'metrics':   {
                        'redis_queue': get_redis_queue(),
                        'http_rate':   http_rate,
                        'http_source': http_source,
                    },
                    'load_test': _load_test.status(),
                }
            except Exception as e:
                payload = {'error': str(e)}
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(INTERVAL)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True, threaded=True)
