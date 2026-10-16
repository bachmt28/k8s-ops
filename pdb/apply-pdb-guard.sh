#!/bin/bash
# Cai dat / cap nhat CronJob pdb-guard. Chay lai duoc nhieu lan.
set -euo pipefail
cd "$(dirname "$0")"

kubectl apply -f pdb-guard.yaml

# Script chi ton tai mot ban trong repo: ConfigMap sinh tu file
kubectl -n k8s-ops-jobs create configmap pdb-guard-script --from-file=pdb-guard.sh \
  --dry-run=client -o yaml | kubectl apply -f -

# Pull secret registry: copy tu namespace da co, khong in noi dung
if ! kubectl -n k8s-ops-jobs get secret nexus-repo-secret >/dev/null 2>&1; then
  kubectl -n "${PULL_SECRET_FROM_NS:-app-node-warmup}" get secret nexus-repo-secret -o yaml \
    | grep -vE "^  (resourceVersion|uid|creationTimestamp|namespace):" \
    | kubectl -n k8s-ops-jobs apply -f -
fi

echo
kubectl -n k8s-ops-jobs get cronjob,cm,secret
echo "Chay thu (chi liet ke, khong xoa):"
echo "  kubectl -n k8s-ops-jobs create job pdb-guard-thu --from=cronjob/pdb-guard --dry-run=client -o yaml \\"
echo "    | kubectl set env --local -f - DRY_RUN=true -o yaml | kubectl apply -f -"
