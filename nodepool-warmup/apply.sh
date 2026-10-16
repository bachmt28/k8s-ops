#!/bin/bash
# Cai dat / cap nhat warm-up node pool. Chay lai duoc nhieu lan.
set -euo pipefail

cd "$(dirname "$0")"

# ConfigMap sinh tu warmup.sh de script chi ton tai mot ban duy nhat trong repo.
kubectl -n app-node-warmup create configmap nodepool-warmup-script \
  --from-file=warmup.sh \
  --dry-run=client -o yaml | kubectl apply -f -

# Ngay le lay dung file scaler exception-ontime dang doc (Jenkins chay nhanh dt), de warm-up va
# scaler cung nghi mot ngay. Sua holidays.txt tren dt xong thi chay lai apply.sh.
git fetch -q origin dt
git show origin/dt:exception-ontime/files/holidays.txt > /tmp/holidays.txt
kubectl -n app-node-warmup create configmap nodepool-warmup-holidays \
  --from-file=holidays.txt=/tmp/holidays.txt \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f manifests.yaml
kubectl apply -f manifests-sbapiv11.yaml

echo
echo "Kiem tra:"
kubectl -n app-node-warmup get cronjob,deploy,cm,sa
