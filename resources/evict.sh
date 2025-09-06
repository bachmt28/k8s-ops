#!/bin/sh



NODE_PATTERNS="$1"     # Regex khớp tên node
DRY_RUN_FLAG="$2"      # --dry-run (tuỳ chọn)
NS_PREFIX=""

MATCHED_NODES_TMP=".matched-nodes.tmp"
> "$MATCHED_NODES_TMP"

IS_DRY_RUN=0
if [ "$DRY_RUN_FLAG" = "--dry-run" ]; then
  IS_DRY_RUN=1
  echo "🔍 Chạy ở chế độ dry-run: KHÔNG xoá pod thật, chỉ in lệnh"
fi

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

  kubectl get pods -A --field-selector spec.nodeName="$NODE" --no-headers \
    | awk -v prefix="$NS_PREFIX" '$1 ~ "^"prefix {print $1, $2}' \
    | grep -Ev 'sb-check|sb-logging|sb-vhht' \
    | while read NS POD; do

      # Kiểm tra annotation unsafe-to-evict
      UNSAFE=$(kubectl get pod "$POD" -n "$NS" -o jsonpath='{.metadata.annotations.unsafe-to-evict}' 2>/dev/null)
      if [ "$UNSAFE" = "true" ]; then
        echo "🚫 Bỏ qua pod $POD (namespace: $NS) vì được đánh dấu unsafe-to-evict=true"
        continue
      fi

      # Xác định loại controller
      OWNER_KIND=$(kubectl get pod "$POD" -n "$NS" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null)

      # Nếu là DaemonSet hoặc Job → bỏ qua
      if [ "$OWNER_KIND" = "DaemonSet" ] ; then
        echo "⏭️  Bỏ qua $OWNER_KIND pod: $POD (namespace: $NS)"
        continue
      fi

      # Mặc định: delete pod (hoặc in lệnh nếu dry-run)
      if [ "$IS_DRY_RUN" -eq 1 ]; then
        echo "[dry-run] kubectl delete pod $POD -n $NS"
      else
        echo "🗑️  Delete pod: $POD (namespace: $NS)"
        kubectl delete pod "$POD" -n "$NS"
      fi
    done

done < "$MATCHED_NODES_TMP"

rm -f "$MATCHED_NODES_TMP"
echo "🏁 Hoàn tất xử lý."
