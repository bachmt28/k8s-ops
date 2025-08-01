#!/bin/sh
echo "### PHASE I - CHECK NODE UTILIZATION"

TMP_FILE=".node-describe.tmp"
NODE_LIST_TMP=".node-list.tmp"
> "$NODE_LIST_TMP"

# === Parse arguments ===
INCLUDE_PATTERN=""
EXCLUDE_PATTERN=""
PROMOTE=false

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
    *)
      echo "❌ Unknown argument: $arg"
      exit 1
      ;;
  esac
done

# === Helper function ===
convert_mem_to_bytes() {
  val=$(echo "$1" | sed 's/[^0-9.]//g')
  unit=$(echo "$1" | sed 's/[0-9.]//g' | tr '[:upper:]' '[:lower:]')
  case "$unit" in
    ki) echo "$(echo "$val * 1024" | bc)" ;;
    mi) echo "$(echo "$val * 1024 * 1024" | bc)" ;;
    gi) echo "$(echo "$val * 1024 * 1024 * 1024" | bc)" ;;
    *) echo "$val" ;;
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

# === Build node list ===
kubectl get nodes --no-headers | awk '{print $1}' > "$NODE_LIST_TMP"

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
AGE_THRESHOLD=3600
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
  echo "⚠️ No eligible nodes older than 1 hour found!"
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

  ALLOC_CPU=$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'cpu:' | awk '{print $2}' | sed 's/m//')
  ALLOC_MEM_RAW=$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'memory:' | awk '{print $2}')
  REQ_CPU=$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  cpu' | awk '{print $2}' | sed 's/m//')
  REQ_MEM_RAW=$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  memory' | awk '{print $2}')

  ALLOC_MEM=$(convert_mem_to_bytes "$ALLOC_MEM_RAW")
  REQ_MEM=$(convert_mem_to_bytes "$REQ_MEM_RAW")

  [ -z "$ALLOC_CPU" ] && ALLOC_CPU=0
  [ -z "$ALLOC_MEM" ] && ALLOC_MEM=0
  [ -z "$REQ_CPU" ] && REQ_CPU=0
  [ -z "$REQ_MEM" ] && REQ_MEM=0

  ALLOC_CPU_CORE=$(echo "scale=2; $ALLOC_CPU / 1000" | bc)
  REQ_CPU_CORE=$(echo "scale=2; $REQ_CPU / 1000" | bc)
  ALLOC_MEM_GiB=$(echo "scale=2; $ALLOC_MEM / 1024 / 1024 / 1024" | bc)
  REQ_MEM_GiB=$(echo "scale=2; $REQ_MEM / 1024 / 1024 / 1024" | bc)

  CPU_RATIO=$(echo "scale=1; if ($ALLOC_CPU_CORE==0) 0 else 100 * $REQ_CPU_CORE / $ALLOC_CPU_CORE" | bc)
  MEM_RATIO=$(echo "scale=1; if ($ALLOC_MEM_GiB==0) 0 else 100 * $REQ_MEM_GiB / $ALLOC_MEM_GiB" | bc)

  IS_UNDERUTILIZED="No"
  if [ "$(echo "$CPU_RATIO < 50.0" | bc)" -eq 1 ] && [ "$(echo "$MEM_RATIO < 50.0" | bc)" -eq 1 ]; then
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
  echo "\n👉 Weakest node (CPU+MEM < 50%): $LOWEST_NODE ($LOWEST_RATIO% CPU)"

  NODEGROUP=$(get_nodegroup_prefix "$LOWEST_NODE")

  # Check against exclude list
  if [ -n "$EXCLUDE_PATTERN" ] && echo "$NODEGROUP" | grep -Eq "$EXCLUDE_PATTERN"; then
    echo "⚠️ Nodegroup [$NODEGROUP] is excluded by pattern [$EXCLUDE_PATTERN]. Aborting eviction."
    exit 0
  fi

  echo "🔍 Checking nodegroup resource availability in group [$NODEGROUP]..."

  TOTAL_FREE_CPU=0
  TOTAL_FREE_MEM=0

  while read NODE; do
    [ "$NODE" = "$LOWEST_NODE" ] && continue
    GROUP_PREFIX=$(get_nodegroup_prefix "$NODE")
    if [ "$GROUP_PREFIX" = "$NODEGROUP" ]; then
      kubectl describe node "$NODE" > "$TMP_FILE"

      ALLOC_CPU=$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'cpu:' | awk '{print $2}' | sed 's/m//')
      ALLOC_MEM_RAW=$(awk '/Allocatable:/,/System Info:/' "$TMP_FILE" | grep -i 'memory:' | awk '{print $2}')
      REQ_CPU=$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  cpu' | awk '{print $2}' | sed 's/m//')
      REQ_MEM_RAW=$(awk '/Allocated resources:/,/Events:/' "$TMP_FILE" | grep -i '^  memory' | awk '{print $2}')

      ALLOC_MEM=$(convert_mem_to_bytes "$ALLOC_MEM_RAW")
      REQ_MEM=$(convert_mem_to_bytes "$REQ_MEM_RAW")

      FREE_CPU=$((ALLOC_CPU - REQ_CPU))
      FREE_MEM=$((ALLOC_MEM - REQ_MEM))

      TOTAL_FREE_CPU=$((TOTAL_FREE_CPU + FREE_CPU))
      TOTAL_FREE_MEM=$((TOTAL_FREE_MEM + FREE_MEM))
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
      ./cordon.sh "$LOWEST_NODE"
      ./evict.sh "$LOWEST_NODE"
    else
      echo "⚠️ Skipping cordon & evict due to missing --promote-evict flag"
    fi
  else
    echo "❌ Nodegroup does NOT have enough resources to evacuate $LOWEST_NODE"
    echo "Free CPU: $TOTAL_FREE_CPU_CORE / Needed: $NEEDED_CPU_CORE (core)"
    echo "Free MEM: $TOTAL_FREE_MEM_GiB / Needed: $NEEDED_MEM_GiB (GiB)"
  fi
else
  echo "\n✅ No underutilized node found (CPU+MEM < 50%)"
fi

rm -f "$NODE_LIST_TMP" "$TMP_FILE"
