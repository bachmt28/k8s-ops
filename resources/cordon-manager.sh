#!/bin/sh
# ----------------------------------------------------------------------
# Cordon / Uncordon node script
# Usage:
#   ./node_cordon.sh --cordon <node1> [node2 ...]
#   ./node_cordon.sh --uncordon <node1> [node2 ...]
# ----------------------------------------------------------------------

# --- Check arguments ---
if [ $# -lt 2 ]; then
  echo "❌ Thiếu tham số!"
  echo "Usage: $0 [--cordon|--uncordon] <node1> [node2 ...]"
  exit 1
fi

ACTION=""
case "$1" in
  --cordon)
    ACTION="cordon"
    ;;
  --uncordon)
    ACTION="uncordon"
    ;;
  *)
    echo "❌ Tham số không hợp lệ: $1"
    echo "Usage: $0 [--cordon|--uncordon] <node1> [node2 ...]"
    exit 1
    ;;
esac

shift  # Bỏ option ra khỏi danh sách đối số

# --- Loop qua từng node ---
for NODE in "$@"; do
  if [ -z "$NODE" ]; then
    continue
  fi

  # Kiểm tra node tồn tại
  if ! kubectl get node "$NODE" >/dev/null 2>&1; then
    echo "❌ Node không tồn tại: $NODE"
    continue
  fi

  # Thực thi hành động
  if [ "$ACTION" = "cordon" ]; then
    echo "🚫 Cordon node: $NODE"
    kubectl cordon "$NODE"
  else
    echo "✅ Uncordon node: $NODE"
    kubectl uncordon "$NODE"
  fi
done
