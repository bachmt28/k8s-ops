#!/bin/bash
# Bu so node con thieu cua node pool bang placeholder pod, roi nhuong dan cho
# workload that khi no preempt. Chi ghi vao deployments/scale cua placeholder.
#
# Ma thoat:
#   0 - binh thuong: pool da du node, hoac giu am toi HOLD_UNTIL, hoac placeholder bi chiem het
#   2 - autoscaler khong cap du node trong WAIT_SCALEUP_SECONDS (van giu am phan da co toi
#       HOLD_UNTIL). Job khong chay lai voi ma nay - xem podFailurePolicy trong manifests.yaml.
#   khac - loi kubectl/API; Job chay lai theo backoffLimit.
set -euo pipefail

POOL="${POOL:-k8s-example-sbapi-1-9}"
TARGET_NODES="${TARGET_NODES:-7}"
NAMESPACE="${NAMESPACE:-app-node-warmup}"
DEPLOY="${DEPLOY:-nodepool-warmup-placeholder}"
HOLD_UNTIL="${HOLD_UNTIL:-09:00}"
WAIT_SCALEUP_SECONDS="${WAIT_SCALEUP_SECONDS:-1800}"
INTERVAL="${INTERVAL:-5}"
# Cung danh sach ngay le voi scaler exception-ontime (apply.sh sinh ConfigMap tu file do): ngay le
# scaler khong bat workload, giu am chi ton VM.
HOLIDAYS_FILE="${HOLIDAYS_FILE:-/opt/holidays/holidays.txt}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

# Chi dung builtin cua bash: image bitnami/kubectl toi gian, khong chac co grep/sed.
is_holiday() {
  local today line
  today="$(date +%F)"
  [ -r "${HOLIDAYS_FILE}" ] || return 1
  while IFS= read -r line || [ -n "${line}" ]; do
    line="${line%%#*}"
    line="${line//[[:space:]]/}"
    [ "${line}" = "${today}" ] && return 0
  done < "${HOLIDAYS_FILE}"
  return 1
}

# Cot STATUS phai dung bang "Ready": loai ca "NotReady" lan
# "Ready,SchedulingDisabled" (node dang bi cordon khong nhan duoc placeholder).
ready_nodes() {
  kubectl get nodes -l "cloud.google.com/gke-nodepool=${POOL}" --no-headers 2>/dev/null \
    | awk '$2 == "Ready" { n++ } END { print n+0 }'
}

# Chi dem pod Running. Pod Pending la pod vua bi preempt dang cho cho moi;
# dem vao se lam vong lap tuong placeholder chua bi cuop.
running_placeholders() {
  kubectl -n "${NAMESPACE}" get pods -l app="${DEPLOY}" --no-headers 2>/dev/null \
    | awk '$3 == "Running" { n++ } END { print n+0 }'
}

# Pod da gan node va khong dang bi xoa = placeholder con giu cho. Dung de quyet dinh HA replicas:
# pod vua gan vao node moi con dang pull image/cho CNI chua Running, dem theo Running se ha nham
# chinh no. Pod bi preempt thi bi xoa, pod thay the nam Pending chua gan node - khong duoc dem.
held_placeholders() {
  kubectl -n "${NAMESPACE}" get pods -l app="${DEPLOY}" --no-headers \
    -o custom-columns=NODE:.spec.nodeName,DEL:.metadata.deletionTimestamp 2>/dev/null \
    | awk '$1 != "<none>" && $2 == "<none>" { n++ } END { print n+0 }'
}

scale_to() {
  kubectl -n "${NAMESPACE}" scale "deploy/${DEPLOY}" --replicas="$1" >/dev/null
}

# Moc ket thuc la gio treo tuong (theo TZ), khong phai "N giay tu luc chay": Job chay lai sau
# khi pod chet van dung dung gio, khong keo dai khung giu am.
if is_holiday; then
  log "$(date +%F) la ngay le (${HOLIDAYS_FILE}), khong warm-up"
  exit 0
fi
if [ ! -r "${HOLIDAYS_FILE}" ]; then
  log "CANH BAO: khong doc duoc ${HOLIDAYS_FILE}, coi nhu khong co ngay le"
fi

hold_end="$(date -d "today ${HOLD_UNTIL}" +%s)"
if [ "$(date +%s)" -ge "${hold_end}" ]; then
  log "da qua ${HOLD_UNTIL}, khong warm-up"
  exit 0
fi

n0="$(ready_nodes)"
need=$(( TARGET_NODES - n0 ))

if [ "${need}" -le 0 ]; then
  log "pool ${POOL} da co ${n0} node Ready (target ${TARGET_NODES}), khong can warm-up"
  exit 0
fi

log "pool ${POOL} co ${n0}/${TARGET_NODES} node Ready, scale ${DEPLOY} len ${need}, giu am toi ${HOLD_UNTIL}"
scale_to "${need}"

scaleup_end=$(( $(date +%s) + WAIT_SCALEUP_SECONDS ))
current="${need}"
phase=1
partial=0
loops=0

while [ "$(date +%s)" -lt "${hold_end}" ]; do
  sleep "${INTERVAL}"
  r="$(running_placeholders)"
  h="$(held_placeholders)"

  if [ "${phase}" -eq 1 ]; then
    n="$(ready_nodes)"
    if [ "${n}" -ge "${TARGET_NODES}" ]; then
      # Dieu kien xong pha 1 la du NODE, khong phai du placeholder Running: workload that co
      # the preempt placeholder ngay trong luc pool dang no - pod bi preempt nam Pending mai.
      log "du ${n}/${TARGET_NODES} node, ${h} placeholder da gan node (${r} Running), chuyen sang pha giu am"
      if [ "${h}" -lt "${current}" ]; then
        scale_to "${h}"
        current="${h}"
      fi
      phase=2
    elif [ "${r}" -ge "${current}" ]; then
      # Du placeholder ma chua du node: placeholder chui vao node cu con trong. Bu them.
      current=$(( current + TARGET_NODES - n ))
      log "${r} placeholder Running nhung pool moi ${n}/${TARGET_NODES} node, bu len ${current}"
      scale_to "${current}"
    elif [ "$(date +%s)" -ge "${scaleup_end}" ]; then
      log "LOI: sau ${WAIT_SCALEUP_SECONDS}s pool moi ${n}/${TARGET_NODES} node, ${r}/${current} placeholder Running - autoscaler khong cap node (xem log cluster-autoscaler). Giu am phan da co."
      scale_to "${h}"
      current="${h}"
      partial=1
      phase=2
    else
      loops=$(( loops + 1 ))
      if [ $(( loops % 12 )) -eq 0 ]; then
        log "cho no: ${r}/${current} placeholder Running, ${n}/${TARGET_NODES} node Ready"
      fi
    fi
    continue
  fi

  # Pha giu am: chi ha replicas, khong bao gio doi lai cho da nhuong.
  # Placeholder con lai tiep tuc giu node trong khoi bi autoscaler thu hoi.
  if [ "${h}" -lt "${current}" ]; then
    log "preempted: ${current} -> ${h}, scale ${DEPLOY} ve ${h}"
    scale_to "${h}"
    current="${h}"
  fi

  if [ "${current}" -le 0 ]; then
    log "placeholder da bi chiem het, ket thuc"
    exit $(( partial * 2 ))
  fi
done

if [ "${phase}" -eq 1 ]; then
  log "LOI: het khung giu am ma pool moi $(ready_nodes)/${TARGET_NODES} node - autoscaler khong cap kip"
  partial=1
fi
scale_to 0
log "het khung giu am ${HOLD_UNTIL}, ha ${DEPLOY} ve 0"
exit $(( partial * 2 ))
