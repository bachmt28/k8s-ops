#!/bin/sh

# Trục xuất pod khỏi các node đang cordoned khớp NODE_PATTERNS, rồi chờ node sạch pod.
#
# Dùng eviction API (policy/v1) thay cho "kubectl delete pod": chỉ eviction mới đi qua
# PodDisruptionBudget. Cluster đang có ~400 PDB, trong đó nhiều PDB đang ở trạng thái
# allowed-disruptions=0 (istio-ingressgateway-bpm, t24adapter-uat-uat, hàng chục service
# trong app-backendapi-dev); delete pod xuyên thẳng qua hết và có thể kéo service xuống
# dưới mức tối thiểu.
#
# Script tự chờ pod rời hẳn node trước khi trả quyền về caller, để caller uncordon đúng
# lúc. Trả về ngay sau khi gọi evict là sai: delete/evict chỉ đánh dấu pod, pod thay thế
# của StatefulSet chỉ được tạo sau khi pod cũ biến mất hẳn — uncordon sớm thì pod quay
# lại đúng node vừa dọn.

NODE_PATTERNS="$1"     # Regex khớp tên node
DRY_RUN_FLAG="$2"      # --dry-run (tuỳ chọn)

DRAIN_TIMEOUT=180      # giây tối đa chờ pod rời node
DRAIN_POLL=10
SKIP_NS_RE='^app-check |^app-logging |^app-vhht '

MATCHED_NODES_TMP=".matched-nodes.tmp"
PODS_TMP=".node-pods.tmp"
EVICT_ERR_TMP=".evict-err.tmp"
> "$MATCHED_NODES_TMP"

IS_DRY_RUN=0
if [ "$DRY_RUN_FLAG" = "--dry-run" ]; then
  IS_DRY_RUN=1
  echo "🔍 Chạy ở chế độ dry-run: KHÔNG evict pod thật, chỉ in danh sách"
fi

# In "<ns> <pod> <ownerKind> <unsafe-to-evict>" cho pod trên node $1 mà script được phép đụng.
#
# Dùng go-template chứ không jsonpath: pod thiếu ownerReferences hoặc thiếu annotations sẽ
# được điền "None"/"false". Với jsonpath các trường đó in ra rỗng, awk dồn cột và giá trị
# annotation bị đọc nhầm thành ownerKind.
#
# Chỉ nhận đúng 4 loại controller biết chắc sẽ tạo lại pod ở chỗ khác. Danh sách cho phép
# chứ không phải danh sách loại trừ: ownerKind lạ thì bỏ qua, vì không có gì bảo đảm pod
# được dựng lại. Ví dụ thật gặp phải là pod shell kubectl của Rancher (cattle-system,
# ownerKind ClusterRole) — evict nó chỉ giết phiên làm việc của người đang dùng.
# Ngoài ra: DaemonSet/Node có tạo lại cũng nằm nguyên tại chỗ, Job giết giữa chừng là mất việc.
list_evictable() {
  kubectl get pods -A --field-selector spec.nodeName="$1" -o go-template='{{range .items}}{{.metadata.namespace}} {{.metadata.name}} {{with .metadata.ownerReferences}}{{(index . 0).kind}}{{else}}None{{end}} {{if .metadata.annotations}}{{with index .metadata.annotations "unsafe-to-evict"}}{{.}}{{else}}false{{end}}{{else}}false{{end}}{{"\n"}}{{end}}' 2>/dev/null \
    | grep -Ev "$SKIP_NS_RE" \
    | awk '$2 != "" && $4 != "true" && ($3 == "ReplicaSet" || $3 == "StatefulSet" || $3 == "Deployment" || $3 == "ReplicationController")'
}

evict_pod() {
  _ns="$1"
  _pod="$2"
  printf '{"apiVersion":"policy/v1","kind":"Eviction","metadata":{"name":"%s","namespace":"%s"}}' "$_pod" "$_ns" \
    | kubectl create --raw "/api/v1/namespaces/$_ns/pods/$_pod/eviction" -f - \
        >/dev/null 2>"$EVICT_ERR_TMP"
}

# Pod mới không thể rơi vào node đang cordoned, nên danh sách này chỉ có thể giảm.
wait_drained() {
  _node="$1"
  _waited=0
  while [ "$_waited" -lt "$DRAIN_TIMEOUT" ]; do
    _left=$(list_evictable "$_node" | wc -l)
    if [ "$_left" -eq 0 ]; then
      echo "✅ Node $_node đã sạch pod sau ${_waited}s."
      return 0
    fi
    sleep "$DRAIN_POLL"
    _waited=$((_waited + DRAIN_POLL))
  done
  echo "⚠️  Hết ${DRAIN_TIMEOUT}s, node $_node vẫn còn $(list_evictable "$_node" | wc -l) pod chưa rời."
  return 1
}

echo "📦 Kiểm tra các node bị cordoned, khớp pattern: $NODE_PATTERNS"

# Lấy danh sách node bị cordoned
ALL_NODES=$(kubectl get nodes --no-headers | awk '$2 == "Ready,SchedulingDisabled" {print $1}')
echo "$ALL_NODES" | grep -E "$NODE_PATTERNS" | sort -u > "$MATCHED_NODES_TMP"

# Nếu không có node nào khớp
if [ ! -s "$MATCHED_NODES_TMP" ]; then
  echo "✅ Không có node nào bị cordoned phù hợp pattern."
  rm -f "$MATCHED_NODES_TMP"
  exit 0
fi

# Xử lý từng node
while read NODE; do
  echo "⚠️  Đang xử lý node: $NODE"

  list_evictable "$NODE" > "$PODS_TMP"

  if [ ! -s "$PODS_TMP" ]; then
    echo "✅ Không có pod nào cần trục xuất trên $NODE."
    continue
  fi

  while read NS POD KIND UNSAFE_FLAG; do
    if [ "$IS_DRY_RUN" -eq 1 ]; then
      echo "[dry-run] evict $KIND pod: $POD (namespace: $NS)"
      continue
    fi

    if evict_pod "$NS" "$POD"; then
      echo "🗑️  Evict $KIND pod: $POD (namespace: $NS)"
    else
      # Thường gặp nhất là 429 "would violate the pod's disruption budget" — đúng ý đồ,
      # để nguyên pod tại chỗ chứ không hạ cấp sang delete.
      echo "🚫 Không evict được $POD (namespace: $NS): $(tr -d '\n' < "$EVICT_ERR_TMP")"
    fi
  done < "$PODS_TMP"

  [ "$IS_DRY_RUN" -eq 1 ] && continue

  wait_drained "$NODE"

done < "$MATCHED_NODES_TMP"

rm -f "$MATCHED_NODES_TMP" "$PODS_TMP" "$EVICT_ERR_TMP"
echo "🏁 Hoàn tất xử lý."
