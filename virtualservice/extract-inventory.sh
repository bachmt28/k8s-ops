#!/bin/bash
set -e

OUT="/tmp/vs-inventory.csv"

kubectl get virtualservice -A -o json \
| jq -r '
  .items[]
  | . as $vs
  | $vs.spec.http[]?
    | .match[]? 
    | [
        $vs.metadata.namespace,
        $vs.metadata.name,
        ($vs.spec.hosts | join(",")),
        ($vs.spec.gateways | join(",")),
        ((.uri.prefix // .uri.exact // .uri.regex // "/") | sub("/+$"; ""))
      ]
  | @csv' | uniq > "$OUT"

echo "[+] Exported VS inventory to $OUT"