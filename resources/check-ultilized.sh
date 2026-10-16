#!/bin/sh
echo "### PHASE I - CHECK NODE UTILIZATION"

TMP_FILE=".node-describe.tmp"
NODE_LIST_TMP=".node-list.tmp"
CORDON_LIST_TMP=".cordoned-by-job.tmp"
> "$NODE_LIST_TMP"

# Node đã gom được giữ cordoned tới lần chạy sau chứ không uncordon ngay sau khi dọn:
# uncordon ngay biến node vừa dọn thành node rỗng nhất pool, scheduler đặt luôn pod thay
# thế quay về đúng nó (build 22883: loki-write-3 quay lại sau 76s). Giữ qua một chu kỳ cron
# thì pod thay thế đã đậu chỗ khác, và cluster-autoscaler có thời gian thu node.
CORDON_LABEL_KEY="ops.example/cordoned-by"
CORDON_LABEL_VAL="check-ultilized"
CORDON_TS_KEY="ops.example/cordoned-at"
MIN_CORDON_HOLD=900

# === Parse arguments ===
INCLUDE_PATTERN=""
EXCLUDE_PATTERN=""
PROMOTE=false
MIN_NODES=0 # NEW: Mặc định bằng 0 tức là không đếm/không giới hạn số node tối thiểu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for arg in "$@"; do
  case "$arg" in
    --include=*)
      INCLUDE_PATTERN="${arg#*=}"
      ;;
    --exclude=*)
      EXCLUDE_PATTERN="${arg#*=}"
      ;;
    --promote-evict)
      PROMOTE=true
      ;;
    --min=*) # NEW: Parse tham số --min
      MIN_NODES="${arg#*=}"
      ;;
    *)
      echo "❌ Unknown argument: $arg"
      exit 1
      ;;
  esac
done

# === Helper function ===
# Đổi một Kubernetes Quantity sang đơn vị nền, nhân thêm hệ số $2
# (1 cho memory → byte, 1000 cho cpu → millicore).
#
# KHÔNG được hạ hoa/thường: 'm' là milli (1e-3) còn 'M' là mega (1e6), 'Mi' là mebi (2^20).
# "kubectl describe node" in tổng request ở đủ 4 dạng tuỳ theo request của pod trên node:
#   28066Mi   20612190Ki   29450329600   31323913728200m
# Dạng milli xuất hiện khi tổng có phân số. Bản cũ hạ hết về chữ thường rồi coi hậu tố lạ
# là byte, nên 31323913728200m bị đọc thành 29172 GiB trên node 29 GiB — sai 1000 lần.
#
# Hậu tố lạ hoặc chuỗi rỗng trả về "ERR" để caller bỏ qua node, thay vì âm thầm tính nhầm.
quantity_to_base() {
  echo "$1" | awk -v scale="$2" '
    {
      if ($0 !~ /^[+-]?[0-9]/) { print "ERR"; exit }
      num  = $0 + 0
      unit = $0
      sub(/^[+-]?[0-9.]+/, "", unit)

      f[""]   = 1
      f["Ki"] = 1024
      f["Mi"] = 1048576
      f["Gi"] = 1073741824
      f["Ti"] = 1099511627776
      f["Pi"] = 1125899906842624
      f["k"]  = 1e3;  f["M"] = 1e6;  f["G"] = 1e9
      f["T"]  = 1e12; f["P"] = 1e15
      f["m"]  = 1e-3; f["u"] = 1e-6; f["n"] = 1e-9

      if (unit in f) printf "%.0f\n", num * f[unit] * scale
      else           print "ERR"
    }'
}

convert_mem_to_bytes() { quantity_to_base "$1" 1; }

# "15757m" → 15757, "13" → 13000. Bản cũ dùng sed 's/m//' nên tổng request ở dạng core
# trần sẽ bị đọc thành millicore, node tụt xuống ~0% và bị chọn evict oan.
convert_cpu_to_millicores() { quantity_to_base "$1" 1000; }

is_num() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

get_nodegroup_prefix() {
  name="$1"
  echo "$name" | awk -F'-' '{
    for (i=1; i<=NF-2; i++) {
      printf "%s%s", $i, (i<NF-2 ? "-" : "")
    }
  }'
}

# Trả lại các node do chính script này cordon ở lần chạy trước.
#
# Chỉ đụng node mang nhãn của script: node ai đó cordon tay để bảo trì không bị động vào.
# Mốc MIN_CORDON_HOLD để hai lần bấm build liên tiếp không tự huỷ việc của nhau — nếu không
# có mốc này, chạy tay lần thứ hai sau 2 phút là node vừa gom đã được mở lại.
release_previous_cordons() {
  _now=$(date +%s)

  kubectl get nodes -l "${CORDON_LABEL_KEY}=${CORDON_LABEL_VAL}" \
    -o go-template='{{range .items}}{{.metadata.name}} {{with index .metadata.annotations "ops.example/cordoned-at"}}{{.}}{{else}}0{{end}}{{"\n"}}{{end}}' \
    2>/dev/null > "$CORDON_LIST_TMP"

  [ -s "$CORDON_LIST_TMP" ] || return 0

  while read _node _ts; do
    [ -n "$_node" ] || continue

    # Mỗi lần gọi chỉ phụ trách phạm vi --include của chính nó, để lượt chạy cho pool này
    # không mở lại node mà lượt chạy cho pool kia vừa cordon trong cùng một build.
    if [ -n "$INCLUDE_PATTERN" ] && ! echo "$_node" | grep -Eq "$INCLUDE_PATTERN"; then
      continue
    fi

    is_num "$_ts" || _ts=0
    _held=$((_now - _ts))

    if [ "$_held" -lt "$MIN_CORDON_HOLD" ]; then
      echo "⏳ Giữ cordon $_node thêm (mới ${_held}s / ${MIN_CORDON_HOLD}s)."
      continue
    fi

    echo "♻️  Trả lại node đã gom lần trước: $_node (đã giữ ${_held}s)"
    sh "${SCRIPT_DIR}/cordon-manager.sh" --uncordon "$_node"
    kubectl label   node "$_node" "${CORDON_LABEL_KEY}-" >/dev/null 2>&1
    kubectl annotate node "$_node" "${CORDON_TS_KEY}-"   >/dev/null 2>&1
  done < "$CORDON_LIST_TMP"

  rm -f "$CORDON_LIST_TMP"
}

release_previous_cordons

# === Build node list ===
# Bỏ node đang cordoned: nó không nhận được pod nên vừa không phải ứng viên để gom, vừa
# không được tính là chỗ trống ở bước kiểm tra sức chứa của nhóm. Gồm cả node người khác
# cordon tay để bảo trì — trước đây chỗ trống của node đó vẫn bị cộng vào, dẫn tới kết luận
# "nhóm đủ chỗ" rồi đuổi pod sang một nơi không thể nhận.
kubectl get nodes -o go-template='{{range .items}}{{if not .spec.unschedulable}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' > "$NODE_LIST_TMP"

if [ -n "$INCLUDE_PATTERN" ]; then
  grep -E "$INCLUDE_PATTERN" "$NODE_LIST_TMP" > .filtered && mv .filtered "$NODE_LIST_TMP"
else
  echo "⚠️ Node pattern group is empty. Auto-grouping nodes based on naming convention."
fi

if [ ! -s "$NODE_LIST_TMP" ]; then
  echo "❌ No matching nodes found!"
  exit 1
fi

# === Filter age >= 1h ===
NOW_TS=$(date +%s)
AGE_THRESHOLD=1200
NODE_LIST_FILTERED=".node-list.filtered"
> "$NODE_LIST_FILTERED"

while read NODE; do
  CREATION_TIME=$(kubectl get node "$NODE" -o jsonpath='{.metadata.creationTimestamp}')
  CREATION_TS=$(date -d "$CREATION_TIME" +%s 2>/dev/null)
  if [ -n "$CREATION_TS" ]; then
    AGE_SEC=$((NOW_TS - CREATION_TS))
    if [ "$AGE_SEC" -ge "$AGE_THRESHOLD" ]; then
      echo "$NODE" >> "$NODE_LIST_FILTERED"
    fi
  fi
done < "$NODE_LIST_TMP"

mv "$NODE_LIST_FILTERED" "$NODE_LIST_TMP"

if [ ! -s "$NODE_LIST_TMP" ]; then
  echo "⚠️ No eligible nodes older than $AGE_THRESHOLD seconds found!"
  exit 0
fi

# === Begin Analysis ===
LOWEST_NODE=""
LOWEST_RATIO=1000
LOWEST_NODE_CPU_REQ=0
LOWEST_NODE_MEM_REQ=0

printf "\n%-40s %-15s %-15s %-10s %-15s %-15s %-10s %-15s\n" "Node" "CPU Req" "CPU Total" "CPU%" "Mem Req (GiB)" "Mem Total" "Mem%" "Underutilized?"
printf "%-40s %-15s %-15s %-10s %-15s %-15s %-10s %-15s\n" \
  "----------------------------------------" "---------------" "---------------" "--------" "---------------" "---------------" "--------" "-----------------"

while read NODE; do
  kubectl describe node "$NODE" > "$TMP_FILE"

  ALLOC_CPU=$(convert_cpu_to_millicores "$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'cpu:' | awk '{print $2}')")
  ALLOC_MEM=$(convert_mem_to_bytes "$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'memory:' | awk '{print $2}')")
  REQ_CPU=$(convert_cpu_to_millicores "$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  cpu' | awk '{print $2}')")
  REQ_MEM=$(convert_mem_to_bytes "$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  memory' | awk '{print $2}')")

  # Thà bỏ sót một node còn hơn gom nhầm vì đọc sai số: node không parse được thì không
  # vào diện ứng viên, và cũng không được tính là chỗ trống ở vòng kiểm tra bên dưới.
  if ! is_num "$ALLOC_CPU" || ! is_num "$ALLOC_MEM" || ! is_num "$REQ_CPU" || ! is_num "$REQ_MEM"; then
    printf "%-40s %-15s %-15s %-10s %-15s %-15s %-10s %-15s\n" \
      "$NODE" "?" "?" "?" "?" "?" "?" "SKIP-parse"
    continue
  fi

  ALLOC_CPU_CORE=$(echo "scale=2; $ALLOC_CPU / 1000" | bc)
  REQ_CPU_CORE=$(echo "scale=2; $REQ_CPU / 1000" | bc)
  ALLOC_MEM_GiB=$(echo "scale=2; $ALLOC_MEM / 1073741824" | bc)
  REQ_MEM_GiB=$(echo "scale=2; $REQ_MEM / 1073741824" | bc)

  # Tính tỉ lệ trên đơn vị nền (millicore/byte), không trên core/GiB đã làm tròn 2 chữ số.
  CPU_RATIO=$(echo "scale=1; if ($ALLOC_CPU==0) 0 else 100 * $REQ_CPU / $ALLOC_CPU" | bc)
  MEM_RATIO=$(echo "scale=1; if ($ALLOC_MEM==0) 0 else 100 * $REQ_MEM / $ALLOC_MEM" | bc)

  IS_UNDERUTILIZED="No"
  if [ "$(echo "$CPU_RATIO < 45.0" | bc)" -eq 1 ] && [ "$(echo "$MEM_RATIO < 45.0" | bc)" -eq 1 ]; then
    IS_UNDERUTILIZED="Yes"
    is_lower=$(echo "$CPU_RATIO < $LOWEST_RATIO" | bc)
    if [ "$is_lower" -eq 1 ]; then
      LOWEST_NODE="$NODE"
      LOWEST_RATIO="$CPU_RATIO"
      LOWEST_NODE_CPU_REQ="$REQ_CPU"
      LOWEST_NODE_MEM_REQ="$REQ_MEM"
    fi
  fi

  printf "%-40s %-15s %-15s %-10s %-15s %-15s %-10s %-15s\n" \
    "$NODE" "$REQ_CPU_CORE" "$ALLOC_CPU_CORE" "${CPU_RATIO}%" "$REQ_MEM_GiB" "$ALLOC_MEM_GiB" "${MEM_RATIO}%" "$IS_UNDERUTILIZED"
done < "$NODE_LIST_TMP"

rm -f "$TMP_FILE"

# === Check if node selected ===
if [ -n "$LOWEST_NODE" ]; then
  echo -e "\n👉 Weakest node (CPU+MEM < 45%): $LOWEST_NODE ($LOWEST_RATIO% CPU)"

  NODEGROUP=$(get_nodegroup_prefix "$LOWEST_NODE")

  # Check against exclude list
  if [ -n "$EXCLUDE_PATTERN" ] && echo "$NODEGROUP" | grep -Eq "$EXCLUDE_PATTERN"; then
    echo "⚠️ Nodegroup [$NODEGROUP] is excluded by pattern [$EXCLUDE_PATTERN]. Aborting eviction."
    exit 0
  fi

  # ==========================================
  # NEW: CHECK TÌNH TRẠNG SỐ LƯỢNG NODE TỐI THIỂU (--min)
  # ==========================================
  if [ "$MIN_NODES" -gt 0 ]; then
    # Đếm xem hiện tại có bao nhiêu node thuộc cùng nhóm NODEGROUP này trong file danh sách node gốc ban đầu
    CURRENT_GROUP_COUNT=0
    while read NODE; do
      GROUP_PREFIX=$(get_nodegroup_prefix "$NODE")
      if [ "$GROUP_PREFIX" = "$NODEGROUP" ]; then
        CURRENT_GROUP_COUNT=$((CURRENT_GROUP_COUNT + 1))
      fi
    done < "$NODE_LIST_TMP"

    echo "🔍 Nhóm [$NODEGROUP] hiện có $CURRENT_GROUP_COUNT node hoạt động (Yêu cầu tối thiểu: $MIN_NODES)"
    
    if [ "$CURRENT_GROUP_COUNT" -le "$MIN_NODES" ]; then
      echo "✅ Bỏ qua trục xuất! Số lượng node hiện tại của nhóm [$NODEGROUP] đang là $CURRENT_GROUP_COUNT (<= $MIN_NODES), để tránh thiếu hụt hệ thống."
      exit 0
    fi
  fi
  # ==========================================

  # ==========================================
  # CHECK IF NODE ONLY HAS DAEMONSET/JOB
  # ==========================================
  echo "🔍 Kiểm tra các pod trên node $LOWEST_NODE..."
  EVICTABLE_PODS=$(kubectl get pods -A --field-selector spec.nodeName="$LOWEST_NODE" \
    -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{" "}{.metadata.ownerReferences[0].kind}{"\n"}{end}' \
    | grep -Ev '^app-check |^app-logging |^app-vhht ' \
    | awk '$3 != "DaemonSet" && $3 != "Job" {print $0}')
  
  if [ -z "$EVICTABLE_PODS" ]; then
    echo "✅ Node $LOWEST_NODE chỉ chứa pod loại DaemonSet/Job (hoặc pod system). Bỏ qua quá trình cordon/evict để tránh loop."
    exit 0
  fi
  # ==========================================

  echo "🔍 Checking nodegroup resource availability in group [$NODEGROUP]..."

  TOTAL_FREE_CPU=0
  TOTAL_FREE_MEM=0

  while read NODE; do
    [ "$NODE" = "$LOWEST_NODE" ] && continue
    GROUP_PREFIX=$(get_nodegroup_prefix "$NODE")
    if [ "$GROUP_PREFIX" = "$NODEGROUP" ]; then
      kubectl describe node "$NODE" > "$TMP_FILE"

      ALLOC_CPU=$(convert_cpu_to_millicores "$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'cpu:' | awk '{print $2}')")
      ALLOC_MEM=$(convert_mem_to_bytes "$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'memory:' | awk '{print $2}')")
      REQ_CPU=$(convert_cpu_to_millicores "$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  cpu' | awk '{print $2}')")
      REQ_MEM=$(convert_mem_to_bytes "$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  memory' | awk '{print $2}')")

      # Node đọc lỗi bị loại khỏi tổng chỗ trống: thiếu chỗ thì cùng lắm là không gom,
      # còn cộng nhầm một số rác vào đây thì đẩy pod sang nhóm không đủ sức chứa.
      if is_num "$ALLOC_CPU" && is_num "$ALLOC_MEM" && is_num "$REQ_CPU" && is_num "$REQ_MEM"; then
        TOTAL_FREE_CPU=$((TOTAL_FREE_CPU + ALLOC_CPU - REQ_CPU))
        TOTAL_FREE_MEM=$((TOTAL_FREE_MEM + ALLOC_MEM - REQ_MEM))
      else
        echo "⚠️  Bỏ qua $NODE khi tính chỗ trống: không parse được số liệu."
      fi
    fi
  done < "$NODE_LIST_TMP"

  TOTAL_FREE_CPU_CORE=$(echo "scale=2; $TOTAL_FREE_CPU / 1000" | bc)
  NEEDED_CPU_CORE=$(echo "scale=2; $LOWEST_NODE_CPU_REQ / 1000" | bc)
  TOTAL_FREE_MEM_GiB=$(echo "scale=2; $TOTAL_FREE_MEM / 1024 / 1024 / 1024" | bc)
  NEEDED_MEM_GiB=$(echo "scale=2; $LOWEST_NODE_MEM_REQ / 1024 / 1024 / 1024" | bc)

  CPU_OK=$(echo "$TOTAL_FREE_CPU >= $LOWEST_NODE_CPU_REQ" | bc)
  MEM_OK=$(echo "$TOTAL_FREE_MEM >= $LOWEST_NODE_MEM_REQ" | bc)

  if [ "$CPU_OK" -eq 1 ] && [ "$MEM_OK" -eq 1 ]; then
    echo "✅ Nodegroup has enough free resources for evacuation."
    if $PROMOTE; then
      sh "${SCRIPT_DIR}/cordon-manager.sh" --cordon "$LOWEST_NODE"

      # Đánh dấu để lần chạy sau biết node này do script cordon và cordon từ lúc nào.
      kubectl label   node "$LOWEST_NODE" "${CORDON_LABEL_KEY}=${CORDON_LABEL_VAL}" --overwrite >/dev/null
      kubectl annotate node "$LOWEST_NODE" "${CORDON_TS_KEY}=$(date +%s)" --overwrite >/dev/null

      # Neo ^...$ để pattern không quét trúng node khác cùng tiền tố.
      sh "${SCRIPT_DIR}/evict.sh" "^${LOWEST_NODE}$"

      # Không uncordon ở đây — release_previous_cordons() ở lần chạy sau sẽ trả node về.
      echo "🔒 Giữ $LOWEST_NODE ở trạng thái cordoned, sẽ trả lại sau ${MIN_CORDON_HOLD}s."

    else
      echo "⚠️ Skipping cordon & evict due to missing --promote-evict flag"
    fi
  else
    echo "❌ Nodegroup does NOT have enough resources to evacuate $LOWEST_NODE"
    echo "Free CPU: $TOTAL_FREE_CPU_CORE / Needed: $NEEDED_CPU_CORE (core)"
    echo "Free MEM: $TOTAL_FREE_MEM_GiB / Needed: $NEEDED_MEM_GiB (GiB)"
  fi
else
  echo -e "\n✅ No underutilized node found (CPU+MEM < 45%)"
fi

rm -f "$NODE_LIST_TMP" "$TMP_FILE" "$CORDON_LIST_TMP"