#!/bin/sh
# Deploys a CI-published commit to EKS: copies its images from GHCR to ECR, installs the load
# balancer controller, then upgrades the app. Run through `make deploy`. POSIX sh: on Windows,
# `bash` can resolve to WSL.
set -eu
cd "$(dirname "$0")/.."

# Keep in step with infra/terraform/policies/.
LBC_VERSION=3.7.0
GHCR=ghcr.io/c-cazworks
NS=pulsequeue

TAG=${1:-$(git rev-parse origin/main)}
case "$TAG" in
  *[!0-9a-f]*) echo "TAG must be a full commit SHA, got '$TAG'" >&2; exit 1 ;;
esac
[ "${#TAG}" -eq 40 ] || { echo "TAG must be a full commit SHA, got '$TAG'" >&2; exit 1; }

tf() { terraform -chdir=infra/terraform output "$@"; }
region=$(tf -raw region)
cluster=$(tf -raw cluster_name)
vpc_id=$(tf -raw vpc_id)
registry=$(tf -raw ecr_registry)
db_host=$(tf -raw db_address)
db_secret=$(tf -raw db_secret_arn)
cidrs=$(tf -json allowed_cidrs)

echo "==> Copying images for $TAG to ECR"
# Captured first: a failed command on the left of a pipe would go unnoticed.
gh_token=$(gh auth token)
gh_user=$(gh api user --jq .login)
printf '%s' "$gh_token" | docker login ghcr.io -u "$gh_user" --password-stdin >/dev/null
ecr_token=$(aws ecr get-login-password --region "$region")
printf '%s' "$ecr_token" | docker login "$registry" -u AWS --password-stdin >/dev/null
for app in api web; do
  dst="$registry/pulsequeue/$app:$TAG"
  if docker buildx imagetools inspect "$dst" >/dev/null 2>&1; then
    echo "$dst already exists"
    continue
  fi
  # prefer-index=false copies the manifest unchanged, so the digest is the one CI tested.
  docker buildx imagetools create --prefer-index=false -t "$dst" "$GHCR/pulsequeue/$app:$TAG"
done

echo "==> Installing AWS Load Balancer Controller $LBC_VERSION"
# region and vpcId are explicit because the IMDS hop limit of 1 blocks pods from looking them up.
# WAF and Shield are off: unused, and not worth the extra API calls on the Free plan. Without the
# Service webhook, a controller outage cannot block creating Services.
helm upgrade --install aws-load-balancer-controller aws-load-balancer-controller \
  --repo https://aws.github.io/eks-charts --version "$LBC_VERSION" -n kube-system \
  --set clusterName="$cluster" --set region="$region" --set vpcId="$vpc_id" \
  --set serviceAccount.create=true --set serviceAccount.name=aws-load-balancer-controller \
  --set replicaCount=1 --set enableServiceMutatorWebhook=false \
  --set enableShield=false --set enableWaf=false --set enableWafv2=false \
  --wait --timeout 5m

echo "==> Deploying the app"
helm upgrade --install pulsequeue deploy/helm -n "$NS" --create-namespace \
  -f deploy/helm/values-aws.yaml \
  --set image.registry="$registry" --set image.tag="$TAG" \
  --set database.host="$db_host" --set database.secretArn="$db_secret" \
  --set-json "ingress.allowedCidrs=$cidrs" \
  --rollback-on-failure --wait --timeout 10m

host=$(kubectl -n "$NS" get ingress pulsequeue -o 'jsonpath={.status.loadBalancer.ingress[0].hostname}')
echo "==> Deployed $TAG: http://$host (DNS can take a few minutes; only your allowed CIDRs can connect)"
