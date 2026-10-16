#!/bin/sh
# get-and-delete-pdb.sh
# Quét tất cả PodDisruptionBudget (PDB) trong một danh sách namespace và xóa.
# Thuần POSIX sh, không phụ thuộc bash.

set -eu

# === Mặc định & biến môi trường ===
KUBECTL="${KUBECTL:-kubectl}"
KUBECTL_TIMEOUT="${KUBECTL_TIMEOUT:-30s}"

DRY_RUN=0
ASSUME_YES=0
NS_FROM_FILE=""
SELECTOR=""

usage() {
  cat <<'EOF'
Usage:
  get-and-delete-pdb.sh [--dry-run] [--yes] [--file=namespaces.txt] [--selector=LABEL_QUERY] [ns1 ns2 ...]

Options:
  --dry-run              Chỉ liệt kê lệnh sẽ chạy, KHÔNG xóa thật.
  --yes                  Bỏ qua xác nhận thủ công (auto-confirm).
  --file=FILE            Đọc danh sách namespace từ FILE (mỗi dòng một namespace, bỏ dòng rỗng/#).
  --selector=QUERY       Xóa chỉ những PDB khớp label selector (ví dụ: "app=emqx").
  -h|--help              Hiển thị trợ giúp.

Ưu tiên nguồn namespace:
  1) Tham số vị trí (ns1 ns2 ...)
  2) --file=...
Nếu cả hai cùng có, sẽ gộp lại (unique theo tên).
Biến môi trường hỗ trợ:
  KUBECTL (mặc định: kubectl), KUBECTL_TIMEOUT (mặc định: 30s)
EOF
}

# === Parse args ===
ARGS_NS=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    --file=*) NS_FROM_FILE="${arg#*=}" ;;
    --selector=*) SELECTOR="${arg#*=}" ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    --*) echo "❌ Unknown option: $arg" >&2; usage; exit 2 ;;
    *) ARGS_NS="$ARGS_NS $arg" ;;
  esac
done

# === Thu thập danh sách namespace ===
NS_LIST_TMP="$(mktemp)"
cleanup() { rm -f "$NS_LIST_TMP"; }
trap cleanup EXIT INT TERM

# Từ file
if [ -n "$NS_FROM_FILE" ]; then
  if [ ! -r "$NS_FROM_FILE" ]; then
    echo "❌ Không đọc được file namespace: $NS_FROM_FILE" >&2
    exit 1
  fi
  # Lọc dòng trống và comment
  sed -e 's/#.*$//' -e 's/^[[:space:]]\+//' -e 's/[[:space:]]\+$//' "$NS_FROM_FILE" \
    | awk 'NF>0' >> "$NS_LIST_TMP"
fi

# Từ args vị trí
# shellcheck disable=SC2086
for ns in $ARGS_NS; do
  echo "$ns" >> "$NS_LIST_TMP"
done

# Unique
if [ ! -s "$NS_LIST_TMP" ]; then
  echo "❌ Chưa cung cấp namespace. Dùng tham số vị trí hoặc --file=..." >&2
  usage
  exit 1
fi
NS_LIST="$(awk '!x[$0]++' "$NS_LIST_TMP")"

# === Hiển thị mục tiêu ===
echo "🎯 Target namespaces:"
echo "$NS_LIST" | sed 's/^/  - /'

if [ -n "$SELECTOR" ]; then
  echo "🔎 Label selector: $SELECTOR"
fi

# === Xác nhận ===
if [ $DRY_RUN -eq 1 ]; then
  echo "🧪 DRY-RUN: sẽ KHÔNG xóa thật."
fi
if [ $ASSUME_YES -ne 1 ]; then
  printf "⚠️  Xác nhận xóa PDB trong các namespace trên? (yes/NO): "
  read ans || ans=""
  case "$ans" in
    y|Y|yes|YES) : ;;
    *) echo "🚫 Huỷ bỏ."; exit 0 ;;
  esac
fi

TOTAL_FOUND=0
TOTAL_DELETED=0

# === Hàm trợ giúp: kiểm tra ns tồn tại ===
ns_exists() {
  ns="$1"
  if "$KUBECTL" get ns "$ns" --request-timeout="$KUBECTL_TIMEOUT" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# === Vòng lặp xử lý từng namespace ===
echo
for ns in $NS_LIST; do
  echo "==== 📦 Namespace: $ns ===="
  if ! ns_exists "$ns"; then
    echo "  ❗ Namespace không tồn tại hoặc không truy cập được, bỏ qua."
    continue
  fi

  GET_ARGS="-n $ns get pdb -o name --request-timeout=$KUBECTL_TIMEOUT"
  if [ -n "$SELECTOR" ]; then
    GET_ARGS="$GET_ARGS -l $SELECTOR"
  fi

  # Liệt kê PDB
  # shellcheck disable=SC2086
  PDBS=$($KUBECTL $GET_ARGS 2>/dev/null || true)

  if [ -z "$PDBS" ]; then
    echo "  (trống) Không có PDB."
    continue
  fi

  COUNT_NS=0
  echo "$PDBS" | while IFS= read -r pdb; do
    [ -z "$pdb" ] && continue
    COUNT_NS=$((COUNT_NS + 1))
    echo "  - $pdb"
  done

  # Đếm tổng tìm thấy
  COUNT_FOUND=$(printf "%s\n" "$PDBS" | awk 'NF>0' | wc -l | tr -d ' ')
  TOTAL_FOUND=$((TOTAL_FOUND + COUNT_FOUND))

  # Xóa
  if [ $DRY_RUN -eq 1 ]; then
    echo "  🧪 DRY-RUN: $KUBECTL -n $ns delete $([ -n "$SELECTOR" ] && printf "pdb -l %s" "$SELECTOR" || printf "pdb --all") --request-timeout=$KUBECTL_TIMEOUT --wait=false"
  else
    if [ -n "$SELECTOR" ]; then
      if "$KUBECTL" -n "$ns" delete pdb -l "$SELECTOR" --request-timeout="$KUBECTL_TIMEOUT" --wait=false; then
        DELETED=$COUNT_FOUND
      else
        DELETED=0
      fi
    else
      if "$KUBECTL" -n "$ns" delete pdb --all --request-timeout="$KUBECTL_TIMEOUT" --wait=false; then
        DELETED=$COUNT_FOUND
      else
        DELETED=0
      fi
    fi
    echo "  ✅ Đã yêu cầu xóa ~${DELETED} PDB."
    TOTAL_DELETED=$((TOTAL_DELETED + DELETED))
  fi

  echo
done

# === Tổng kết ===
if [ $DRY_RUN -eq 1 ]; then
  echo "📊 Tổng kết (DRY-RUN): phát hiện $TOTAL_FOUND PDB trong các namespace mục tiêu."
else
  echo "📊 Tổng kết: phát hiện $TOTAL_FOUND PDB, đã yêu cầu xóa $TOTAL_DELETED PDB."
fi

exit 0
