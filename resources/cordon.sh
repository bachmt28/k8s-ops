#!/bin/sh

# Kiểm tra đối số
if [ -z "$1" ]; then
  echo "❌ Thiếu tên node để cordon!"
  echo "Usage: $0 <node-name>"
  exit 1
fi

NODE="$1"

# Kiểm tra node có tồn tại không
kubectl get node "$NODE" >/dev/null 2>&1
if [ $? -ne 0 ]; then
  echo "❌ Node không tồn tại: $NODE"
  exit 1
fi

# Cordon node
echo "🚫 Cordon node: $NODE"

for NODE in "$@"; do
  [ -n "$NODE" ] && kubectl cordon "$NODE"
done

