#!/usr/bin/env bash
# Borra todo lo que crea el camino AWS de la demo.
#
#   CUENTA=<tu account id> DOMINIO=aura.example.com bash aws/teardown.sh 2>&1 | tee teardown.log
#
# Orden: rutas y Gateway (Auto Mode borra el ALB) → esperar que no quede el ALB →
# cluster con eksctl (borra nodos, VPC, NAT, subredes y roles que creó) → ECR →
# certificado ACM → verificación. Los registros de Cloudflare se borran a mano.
#
# No se desinstalan los charts de KEDA antes de eksctl: no tienen Services
# LoadBalancer, y eksctl puede fallar si Helm borra un Service al mismo tiempo. Idempotente: cada paso tolera que el recurso ya no exista.
set -u
cd "$(dirname "$0")/.."

ACCOUNT=${CUENTA:?Define CUENTA con tu account id: evita borrar en otra cuenta por error}
DOMINIO=${DOMINIO:-aura.example.com}
REGION=us-east-1
CLUSTER=keda-http

paso() { printf '\n\033[1;33m== %s  (%s)\033[0m\n' "$1" "$(date '+%H:%M:%S')"; }

paso "0. Identidad"
ID=$(aws sts get-caller-identity --query Account --output text)
echo "cuenta: $ID"
if [ "$ID" != "$ACCOUNT" ]; then echo "La cuenta no es $ACCOUNT. Me detengo."; exit 1; fi

paso "1. Rutas y Gateway (Auto Mode borra el ALB)"
if aws eks describe-cluster --region $REGION --name $CLUSTER >/dev/null 2>&1; then
  aws eks update-kubeconfig --region $REGION --name $CLUSTER >/dev/null
  kubectl -n keda-app delete httproute --all --ignore-not-found || true
  kubectl -n keda-app delete gateway alb-gateway --ignore-not-found --wait=false || true
else
  echo "  el cluster ya no existe"
fi

paso "2. Esperar a que Auto Mode borre el ALB (hasta 6 min)"
for i in $(seq 1 24); do
  N=$(aws elbv2 describe-load-balancers --region $REGION \
    --query "length(LoadBalancers[?VpcId=='$(aws eks describe-cluster --region $REGION --name $CLUSTER --query cluster.resourcesVpcConfig.vpcId --output text 2>/dev/null)'])" \
    --output text 2>/dev/null || echo "?")
  echo "  balanceadores en la VPC del cluster: $N"
  [ "$N" = "0" ] && break
  sleep 15
done

paso "3. Cluster (eksctl, 10 a 20 min)"
eksctl delete cluster --region $REGION --name $CLUSTER --wait --disable-nodegroup-eviction 2>&1 || {
  echo "  eksctl falló; relanzando una vez (suele bastar con una segunda pasada)"
  eksctl delete cluster --region $REGION --name $CLUSTER --wait --disable-nodegroup-eviction 2>&1 || true
}

paso "4. Repositorio ECR"
aws ecr delete-repository --region $REGION --repository-name keda-http-aura --force \
  --query repository.repositoryName --output text 2>&1 || true

paso "5. Certificado ACM de $DOMINIO"
for arn in $(aws acm list-certificates --region $REGION \
    --query "CertificateSummaryList[?DomainName=='$DOMINIO'].CertificateArn" --output text); do
  aws acm delete-certificate --region $REGION --certificate-arn "$arn" && echo "  borrado $arn" || true
done

paso "6. Stacks de CloudFormation que pudieran quedar"
for s in $(aws cloudformation list-stacks --region $REGION --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED \
    --query "StackSummaries[?starts_with(StackName,'eksctl-$CLUSTER')].StackName" --output text); do
  echo "  stack $s todavía existe; intentando borrarlo"
  aws cloudformation delete-stack --region $REGION --stack-name "$s" || true
done

paso "7. Verificación final (todo debería estar vacío)"
echo "  instancias:     $(aws ec2 describe-instances --region $REGION --filters Name=tag:eks:eks-cluster-name,Values=$CLUSTER --query 'Reservations[].Instances[?State.Name!=`terminated`].InstanceId' --output text)"
echo "  balanceadores:  $(aws elbv2 describe-load-balancers --region $REGION --query 'LoadBalancers[].LoadBalancerName' --output text)"
echo "  NAT:            $(aws ec2 describe-nat-gateways --region $REGION --filter Name=state,Values=available --query 'NatGateways[].NatGatewayId' --output text)"
echo "  volúmenes:      $(aws ec2 describe-volumes --region $REGION --query 'Volumes[].VolumeId' --output text)"
echo "  VPC no default: $(aws ec2 describe-vpcs --region $REGION --query 'Vpcs[?IsDefault==`false`].VpcId' --output text)"
echo "  clusters EKS:   $(aws eks list-clusters --region $REGION --query 'clusters' --output text)"
echo "  stacks eksctl:  $(aws cloudformation list-stacks --region $REGION --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED DELETE_IN_PROGRESS --query "StackSummaries[?starts_with(StackName,'eksctl-$CLUSTER')].StackName" --output text)"
echo "  ECR:            $(aws ecr describe-repositories --region $REGION --repository-names keda-http-aura --query 'repositories[0].repositoryName' --output text 2>&1 | head -1)"
echo "  certificado:    $(aws acm list-certificates --region $REGION --query "CertificateSummaryList[?DomainName=='$DOMINIO'].CertificateArn" --output text)"

paso "Falta a mano: en tu DNS, borrar los CNAME de la app, del panel y los _xxx de validación de ACM"
paso "Listo. Si algo quedó en la verificación, bórralo desde la consola"
