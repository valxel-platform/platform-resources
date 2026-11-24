#!/usr/bin/env bash
# Apply the e2e resources, wait until Crossplane says they're ready, and check moto has them.
#   local/aws/e2e/check.sh <kubectl context> [moto container]
set -euo pipefail
ctx=$1 moto=${2:-valxel-moto} here=$(cd "$(dirname "$0")" && pwd)
k() { kubectl --context "$ctx" "$@"; }
k apply -f "$here/resources.yaml"
kinds="workloadidentities.aws.platform.valxel.dev keyvaluetables.platform.valxel.dev objectbuckets.platform.valxel.dev queues.platform.valxel.dev topics.platform.valxel.dev eventstreams.platform.valxel.dev"
ok=1
for kind in $kinds; do
  k -n valxel-e2e wait "$kind" --all --for=condition=Ready --timeout=15m || ok=0
done
if [ $ok = 0 ]; then
  echo "::group::What isn't ready"
  k get managed -A -o custom-columns='KIND:.kind,NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,SYNCED:.status.conditions[?(@.type=="Synced")].status,WHY:.status.conditions[?(@.type=="Synced")].message' || true
  k -n valxel-e2e get $(echo $kinds | tr ' ' ',') -o yaml | grep -A4 "type: Ready\|type: Synced" || true
  echo "::endgroup::"
fi
docker exec -i "$moto" python3 - < "$here/check.py"
[ $ok = 1 ]
