#!/usr/bin/env bash
# Levanta la demo completa en minikube: sin AWS, sin dominio, sin costo.
# Requisitos: docker, minikube, kubectl y helm.
set -euo pipefail
cd "$(dirname "$0")/.."

PERFIL=${PERFIL:-keda-local}
KEDA_VERSION=2.21.0
HTTP_ADDON_VERSION=0.15.0

echo "==> 1/5 Cluster minikube ($PERFIL)"
minikube status -p "$PERFIL" >/dev/null 2>&1 || minikube start -p "$PERFIL" --cpus=4 --memory=6g
kubectl config use-context "$PERFIL" >/dev/null

echo "==> 2/5 Imágenes de la app, construidas dentro de minikube"
eval "$(minikube -p "$PERFIL" docker-env)"
docker build -q -t aura-vote:local app/vote
docker build -q -t aura-worker:local app/worker
docker build -q -t aura-dashboard:local app/dashboard

echo "==> 3/5 KEDA $KEDA_VERSION y HTTP Add-on $HTTP_ADDON_VERSION"
helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
helm repo update kedacore >/dev/null
helm upgrade --install keda kedacore/keda -n keda --create-namespace --version "$KEDA_VERSION" --wait
helm upgrade --install http-add-on kedacore/keda-add-ons-http -n keda --version "$HTTP_ADDON_VERSION" \
  -f keda/values-http-add-on.yaml --wait

echo "==> 4/5 La app"
kubectl apply -k k8s/local
# Código que pide el panel para generar carga o reiniciar votos. En local alcanza con uno fijo.
kubectl -n keda-app create secret generic dashboard-demo --from-literal=code=local \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n keda-app rollout status deploy/k8s-dashboard --timeout=180s

echo "==> 5/5 Listo. En dos terminales aparte:"
cat <<'MSG'

  kubectl -n keda port-forward svc/keda-add-ons-http-interceptor-proxy 8080:8080
      → la app: http://aura.localhost:8080   (empieza en 0 pods: el primer voto la despierta)

  kubectl -n keda-app port-forward svc/k8s-dashboard 8081:80
      → el panel: http://localhost:8081      (código para generar carga: local)

Para borrar todo: local/down.sh
MSG
