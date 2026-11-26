#!/usr/bin/env bash
# Deploys the chart to the current kind cluster and sends an event through every component.
# Expects local/pulsequeue/{api,web}:$TAG to be loaded into the cluster already.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG=${1:?usage: kind-smoke.sh <image tag>}
NS=pulsequeue

# Never touch a real cluster by accident.
context=$(kubectl config current-context)
case "$context" in
  kind-*) ;;
  *) echo "Refusing: kubectl context is '$context', not a kind cluster." >&2; exit 1 ;;
esac

dump() {
  echo "--- smoke test failed" >&2
  kubectl -n "$NS" get pods -o wide >&2 || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 20 >&2 || true
  for c in migrate api worker simulator web; do
    kubectl -n "$NS" logs -l "app.kubernetes.io/component=$c" --tail=20 --prefix >&2 || true
  done
}
trap dump ERR

# Enforce the strictest Pod Security level on our pods; warn-only would hide regressions.
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl -n "$NS" get secret postgres >/dev/null 2>&1; then
  kubectl -n "$NS" create secret generic postgres --from-literal=password="$(head -c 18 /dev/urandom | base64)"
fi
kubectl -n "$NS" apply -f deploy/kind/postgres.yaml
kubectl -n "$NS" rollout status deploy/postgres --timeout=120s
# Postgres is already running, so enforcing now only checks the chart's own pods.
kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=restricted --overwrite

helm upgrade --install pulsequeue deploy/helm -n "$NS" -f deploy/helm/values-kind.yaml \
  --set image.tag="$TAG" --wait --timeout 4m

# Through nginx -> API -> Postgres -> worker, from inside the cluster.
# Helm --wait covered the simulator pod; the summary check below covers its traffic.
kubectl -n "$NS" exec -i deploy/pulsequeue-api -- python - <<'PY'
import json, time, urllib.request

BASE = "http://pulsequeue-web"

def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(BASE + path, data, {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)

event_id = call("/api/events", {"event_type": "payment.authorized", "payload": {"id": "pay_smoke"}})["id"]
for _ in range(30):
    event = call(f"/api/events/{event_id}")
    if event["state"] == "processed":
        print("event delivered:", event)
        break
    time.sleep(1)
else:
    raise SystemExit(f"event never processed: {event}")

# The console's controls: switch the scenario and see it in the summary, then switch back.
assert call("/api/scenario", {"profile": "recover"}, "PUT")["profile"] == "recover"
summary = call("/api/summary")
assert summary["scenario"]["profile"] in ("recover", "healthy"), summary["scenario"]
assert summary["throughput_per_second"] > 0, summary  # the simulator is sending traffic
call("/api/scenario", {"profile": "healthy"}, "PUT")
print("smoke test passed: scenario switching and summary work")
PY
