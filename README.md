# Escalando apps en Kubernetes en base a requests HTTP/s

Demo y slides de la charla que di en el **AWS Student Community Day 2026**
(UTN FRC, Córdoba, 14/11/2026). Slides online:
[lucasdevops.cloud/charlas/utn-frc-escalando-apps-http](https://lucasdevops.cloud/charlas/utn-frc-escalando-apps-http/).

Una app de votación, **"¿Quién tiene más aura?"**, que arranca en **0 pods** y
escala según los **pedidos por segundo** que recibe, con
[KEDA](https://keda.sh) y su [HTTP Add-on](https://github.com/kedacore/http-add-on).
Un panel muestra en vivo los pods, los nodos, los req/s y los votos, y trae un
generador de carga.

```
navegador ──► interceptor de KEDA ──► vote (0 → 10 pods) ──► Redis ──► worker (0 → 10 pods) ──► Postgres
                    │                                          │
                    └── req/s ──► KEDA ◄── largo de la cola ───┘
                                   decide cuántos pods
```

- **vote** escala por req/s: un pod cada 10 req/s, de 0 a 10.
- **worker** escala por el largo de la cola de Redis, de 0 a 10.
- Si no hay tráfico, el interceptor retiene el primer pedido hasta que arranca
  un pod (cold start) y después lo entrega.

Hay dos formas de correrla:

| | Local (minikube) | AWS (EKS Auto Mode) |
|---|---|---|
| Costo | gratis | por hora mientras esté prendido |
| Cómo entra el tráfico | `kubectl port-forward` al interceptor | ALB creado por Auto Mode desde un `Gateway` (Gateway API) |
| Nodos | uno fijo | Auto Mode agrega y saca nodos según los pods pendientes |
| Tiempo para levantarla | ~5 min | ~45 min |

## Estructura

```
app/            código de las tres apps (Python + Flask): vote, worker, dashboard
k8s/base/       manifiestos comunes: datos, app, HTTPScaledObject, ScaledObject, panel
k8s/local/      overlay para minikube (imágenes locales, host aura.localhost)
k8s/aws/        overlay para EKS: GatewayClass, Gateway, HTTPRoutes, config del ALB
keda/           values del HTTP Add-on
local/          up.sh y down.sh para el camino local
aws/            cluster de eksctl, NodePool Spot y teardown.sh
slides/         el deck de la charla (reveal.js): abre slides/index.html en el navegador
```

## Camino 1: en tu máquina con minikube

Requisitos: [Docker](https://docs.docker.com/get-docker/),
[minikube](https://minikube.sigs.k8s.io/docs/start/),
[kubectl](https://kubernetes.io/docs/tasks/tools/) y
[Helm](https://helm.sh/docs/intro/install/). Unos 4 CPU y 6 GB de RAM libres.

```bash
./local/up.sh
```

Crea el cluster `keda-local`, construye las imágenes dentro de minikube,
instala KEDA y el HTTP Add-on, y aplica `k8s/local`. Después, en dos terminales:

```bash
kubectl -n keda port-forward svc/keda-add-ons-http-interceptor-proxy 8080:8080
kubectl -n keda-app port-forward svc/k8s-dashboard 8081:80
```

- La app: <http://aura.localhost:8080>. Empieza en 0 pods; el primer pedido la
  despierta. `*.localhost` resuelve solo a tu máquina y es el host que el
  interceptor usa para saber a qué app mandar el pedido.
- El panel: <http://localhost:8081>. Para generar carga o reiniciar los votos
  pide un código: en local es `local`.

Qué probar:

1. Mira el panel con `vote` en 0 pods y entrá a la app: aparece un pod.
2. En el panel, genera carga con 20 o 40 pedidos simultáneos: suben las
   réplicas de `vote` y de `worker`.
3. Detén la carga: en uno o dos minutos los dos vuelven a 0.

Para borrar todo: `./local/down.sh`.

## Camino 2: en AWS con EKS Auto Mode y Gateway API

Usa la función de EKS Auto Mode que crea el balanceador a partir de recursos de
Gateway API ([anuncio](https://aws.amazon.com/about-aws/whats-new/2026/10/amazon-eks-auto-mode-kubernetes-gateway-api/),
[documentación](https://docs.aws.amazon.com/eks/latest/userguide/auto-configure-gateway-api.html)).

> **Cuesta plata mientras esté prendido**: control plane de EKS, nodos, NAT
> Gateway y ALB ([precios de EKS](https://aws.amazon.com/eks/pricing/),
> [ELB](https://aws.amazon.com/elasticloadbalancing/pricing/),
> [VPC](https://aws.amazon.com/vpc/pricing/)). Usa una cuenta propia, pon una
> alarma de presupuesto y corre el teardown al terminar.

Requisitos: AWS CLI, [eksctl](https://eksctl.io), kubectl, Helm, Docker con
buildx, un dominio donde puedas crear registros CNAME.

**1. Cluster** (unos 20 minutos). Reemplaza `<TU_IP_PUBLICA>` en
`aws/cluster.yaml`: la API del cluster solo va a aceptar tu IP.

```bash
eksctl create cluster -f aws/cluster.yaml
```

**2. Gateway API administrada por Auto Mode, y el NodePool Spot de la app.**

```bash
aws eks update-cluster-config --region us-east-1 --name keda-http \
  --kubernetes-network-config '{"elasticLoadBalancing":{"enabled":true,"gatewayApi":{"crds":{"managed":true}}}}'
aws eks wait cluster-active --region us-east-1 --name keda-http
kubectl apply -f aws/nodepool-demo-spot.yaml
```

Si tu AWS CLI responde `Unknown parameter ... gatewayApi`, es anterior a la
función. Actualízalo o haz el mismo llamado con boto3 reciente:

```bash
python3 -c "import boto3; boto3.client('eks', region_name='us-east-1').update_cluster_config(name='keda-http', kubernetesNetworkConfig={'elasticLoadBalancing': {'enabled': True, 'gatewayApi': {'crds': {'managed': True}}}})"
```

**3. Certificado.** Pide un certificado de ACM para tus dos hosts (la app y el
panel) y valídalo con los CNAME que te da ACM en tu DNS.

```bash
aws acm request-certificate --region us-east-1 --validation-method DNS \
  --domain-name aura.example.com --subject-alternative-names aura-panel.example.com
```

**4. Imágenes** multi-arch en ECR (el NodePool mezcla arm64 y amd64).

```bash
REG=<TU_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
aws ecr create-repository --region us-east-1 --repository-name keda-http-aura
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REG
for a in vote worker dashboard; do
  docker buildx build --platform linux/amd64,linux/arm64 -t $REG/keda-http-aura:$a-v1 --push app/$a
done
```

**5. KEDA y el HTTP Add-on.**

```bash
helm repo add kedacore https://kedacore.github.io/charts && helm repo update kedacore
helm install keda kedacore/keda -n keda --create-namespace --version 2.21.0
helm install http-add-on kedacore/keda-add-ons-http -n keda --version 0.15.0 -f keda/values-http-add-on.yaml
```

**6. La app, el Gateway y las rutas.** Reemplaza los valores que lista
`k8s/aws/kustomization.yaml` (dominios, ARN del certificado, account id) y:

```bash
kubectl apply -k k8s/aws
kubectl -n keda-app create secret generic dashboard-demo --from-literal=code=<elige-un-código>
kubectl -n keda-app get gateway alb-gateway   # ADDRESS = DNS del ALB, PROGRAMMED=True en ~2 min
```

**7. DNS.** Dos CNAME (la app y el panel) apuntando al `ADDRESS` del Gateway.
Si usas Cloudflare, en modo *DNS only*: el TLS lo termina el ALB.

Qué validar:

- `kubectl -n keda-app describe httproute vote-route` → `Accepted` y
  `ResolvedRefs` en `True`. La ruta cruza al namespace `keda` gracias al
  `ReferenceGrant`.
- `http://` responde 301 a `https://`.
- Con carga, aparecen pods `Pending` y Auto Mode agrega un nodo Spot.

**Teardown:**

```bash
CUENTA=<TU_ACCOUNT_ID> DOMINIO=aura.example.com bash aws/teardown.sh
```

Borra las rutas y el Gateway (Auto Mode borra el ALB), el cluster, el
repositorio de ECR y el certificado, y al final lista lo que quede. Los
registros de DNS se borran a mano.

## Lo que medí en el ensayo (EKS, 13/11/2026)

| | |
|---|---|
| Cold start de 0 a 1 pod, con nodo disponible | 5,4 s |
| `vote` de 0 a 10 pods con ~100 req/s | ~70 s |
| Nodo Spot nuevo listo (pods Pending desde los ~24 s) | ~46 s |
| De vuelta a 0 al cortar la carga | `worker` ~70 s, `vote` ~95 s |

## Detalles

- Versiones: Kubernetes 1.36 en EKS, KEDA 2.21.0
  ([matriz de compatibilidad](https://keda.sh/docs/2.21/operate/cluster/)),
  HTTP Add-on 0.15.0. Desde la 0.14 el add-on marca `HTTPScaledObject` como
  deprecado en favor de `InterceptorRoute`, pero sigue soportado
  ([release 0.14.0](https://github.com/kedacore/http-add-on/releases/tag/v0.14.0)).
- `granularity: 1s` en `k8s/base/vote-httpscaledobject.yaml` no es un detalle.
  En la 0.15 el scaler promedia los pedidos **por intervalo de `granularity`**,
  no por segundo ([`WindowAverage`](https://github.com/kedacore/http-add-on/blob/v0.15.0/pkg/queue/bucketing.go)),
  aunque la [documentación](https://keda.sh/http-add-on/0.15/user-guide/configure-scaling/)
  habla de "requests per second". Con `granularity: 10s` y `targetValue: 10`,
  20 req/s reales se miden como ~200 y la app sube al máximo. Con `1s`,
  `targetValue` significa de verdad req/s por pod. Lo comprobé midiendo el HPA
  con tráfico conocido.
- Postgres y Redis no tienen persistencia: es una demo, si se reinician se
  pierden los votos.
- `k8s/base/worker.yaml` trae credenciales de demo (`postgres/postgres`) en
  texto plano a propósito. En algo real van en un gestor de secretos.
- Las imágenes de los candidatos son ilustraciones genéricas
  (`app/vote/static/images/opcion-a.svg` y `opcion-b.svg`). Reemplázalas por
  las tuyas con el mismo nombre, y cambia los nombres con `OPTION_A` y
  `OPTION_B` en `k8s/base/vote.yaml` y `k8s/base/dashboard.yaml`.

Basado en [example-voting-app](https://github.com/dockersamples/example-voting-app)
de Docker. Licencia MIT.
