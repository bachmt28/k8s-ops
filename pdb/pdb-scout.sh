#!/bin/sh
# pdb-scout.sh
# Liệt kê PDB "không an toàn" theo danh sách namespace đầu vào
# và gợi ý HÀNH ĐỘNG CẤU HÌNH BỀN VỮNG (không đề xuất scale/drain tạm thời).
# Yêu cầu: kubectl, jq
# Usage:
#   ./pdb-scout.sh ns1 ns2 ...
#   ./pdb-scout.sh -f namespaces.txt
set -eu

if [ $# -eq 0 ]; then
  echo "Usage: $0 <ns1> [ns2 ...] | -f <ns_file>" >&2
  exit 1
fi

NS_LIST_TMP=".pdb-scout.ns.tmp"
> "$NS_LIST_TMP"

if [ "$1" = "-f" ]; then
  [ $# -ge 2 ] || { echo "Need a file after -f" >&2; exit 1; }
  awk 'NF {print $1}' "$2" | sed 's/\r$//' >> "$NS_LIST_TMP"
  shift 2 || true
fi

for ns in "$@"; do
  echo "$ns" >> "$NS_LIST_TMP"
done

sort -u "$NS_LIST_TMP" | awk 'NF' > "$NS_LIST_TMP.sorted"
mv "$NS_LIST_TMP.sorted" "$NS_LIST_TMP"

# Header CSV
echo "namespace,pdb,policy,selector,currentHealthy,desiredHealthy,allowed,totalPods,risk_flags,recommendation"

TOTAL_RISK=0

build_selector() {
  jq -r '
    (.spec.selector // {}) as $sel
    | ($sel.matchLabels // {}) as $ml
    | ($ml | to_entries | map("\(.key)=\(.value)")) as $l
    | ($sel.matchExpressions // []) as $me
    | ($me | map(
        if .operator=="In" then "\(.key) in (\(.values|join(",")))"
        elif .operator=="NotIn" then "\(.key) notin (\(.values|join(",")))"
        elif .operator=="Exists" then "\(.key)"
        elif .operator=="DoesNotExist" then "!\(.key)"
        else "UNSUPPORTED"
        end
      )) as $e
    | ([$l[], $e[]] | map(select(length>0 and .!="UNSUPPORTED")) | join(",")) as $selstr
    | $selstr
  '
}

has_unsupported_expr() {
  jq -e '
    (.spec.selector.matchExpressions // [])
    | map(.operator) | any(. != "In" and . != "NotIn" and . != "Exists" and . != "DoesNotExist")
  ' >/dev/null 2>&1
}

recommend_for_flags() {
  # args: NS NAME POLICY MIN_AVAIL MAX_UNAVAIL ALLOWED TOTAL_PODS RISK
  NS="$1"; NAME="$2"; POLICY="$3"; MIN_AVAIL="$4"; MAX_UNAVAIL="$5"; ALLOWED="$6"; TOTAL_PODS="$7"; RISK="$8"
  RECO=""

  append() { [ -n "$RECO" ] && RECO="$RECO; "; RECO="$RECO$1"; }

  case "$RISK" in *ns_not_found*)
    append "Namespace not found";;
  esac

  case "$RISK" in *selector_empty*)
    append "Add .spec.selector to PDB to correctly match workloads (matchLabels/matchExpressions)";;
  esac

  case "$RISK" in *orphan*)
    append "PDB does not match any pods: delete PDB or fix selector to match workload labels";;
  esac

  case "$RISK" in *expr_unsupported*)
    append "Simplify selector: only use In/NotIn/Exists/DoesNotExist; avoid unsupported operators";;
  esac

  case "$RISK" in *max0*)
    append "Set .spec.maxUnavailable=1 (avoid 0, which blocks all evictions)";;
  esac

  case "$RISK" in *tight_min*)
    # Sustainable configuration:
    # - If using minAvailable: set minAvailable = replicas-1 (always leave eviction room)
    # - Or switch to maxUnavailable=1 (recommended default for replicas >=2)
    if [ "${TOTAL_PODS:-0}" -ge 2 ]; then
      if [ "$POLICY" = "minAvailable" ]; then
        append "Set .spec.minAvailable = replicas-1 to allow evictions, or switch to .spec.maxUnavailable=1"
      else
        append "Confirm policy: prefer .spec.maxUnavailable=1 for workloads with replicas >=2"
      fi
    else
      append "Single-replica workload: avoid PDB, or commit to replicas >=2 and then use .spec.maxUnavailable=1"
    fi
    ;;
  esac

  case "$RISK" in *allowed0*)
    # Sustainable fix:
    # - For maxUnavailable PDBs: use .spec.maxUnavailable=1 (not 0) and ensure replicas/HPA.minReplicas >=2
    # - For minAvailable PDBs: set minAvailable <= replicas-1 and align HPA.minReplicas >= minAvailable+1
    if [ "$POLICY" = "maxUnavailable" ]; then
      append "Ensure .spec.maxUnavailable=1 (not 0) and replicas/HPA.minReplicas >=2"
    elif [ "$POLICY" = "minAvailable" ]; then
      append "Set .spec.minAvailable <= replicas-1 and ensure HPA.minReplicas >= minAvailable+1; consider switching to .spec.maxUnavailable=1"
    else
      append "Define PDB explicitly: recommended .spec.maxUnavailable=1 for workloads with replicas >=2"
    fi
    ;;
  esac

  case "$RISK" in *config_mismatch*)
    append "Align PDB with HPA and rolling strategy: HPA.minReplicas >= minAvailable+1; Deployment.rollingUpdate.maxUnavailable >=1; prefer PDB .spec.maxUnavailable=1"
    ;;
  esac



  [ -z "$RECO" ] && RECO="ok"
  printf '%s' "$RECO"
}

while read -r NS; do
  if ! kubectl get ns "$NS" >/dev/null 2>&1; then
    printf '%s,%s,%s,"%s",%s,%s,%s,%s,%s,"%s"\n' \
      "$NS" "*" "*" "*" "*" "*" "*" "*" "ns_not_found" "Namespace not found" >&2
    TOTAL_RISK=$((TOTAL_RISK+1))
    continue
  fi

  PDB_JSON=$(kubectl get pdb -n "$NS" -o json 2>/dev/null || echo '{"items":[]}')
  COUNT=$(printf '%s' "$PDB_JSON" | jq '.items | length')
  [ "$COUNT" -eq 0 ] && continue

  idx=0
  while [ "$idx" -lt "$COUNT" ]; do
    ONE=$(printf '%s' "$PDB_JSON" | jq ".items[$idx]")
    NAME=$(printf '%s' "$ONE" | jq -r '.metadata.name')
    POLICY=$(printf '%s' "$ONE" | jq -r 'if .spec.minAvailable != null then "minAvailable"
                                         elif .spec.maxUnavailable != null then "maxUnavailable"
                                         else "none" end')
    MIN_AVAIL=$(printf '%s' "$ONE" | jq -r '.spec.minAvailable // empty')
    MAX_UNAVAIL=$(printf '%s' "$ONE" | jq -r '.spec.maxUnavailable // empty')
    CURR_HEALTHY=$(printf '%s' "$ONE" | jq -r '.status.currentHealthy // 0')
    DESIRED_HEALTHY=$(printf '%s' "$ONE" | jq -r '.status.desiredHealthy // 0')
    ALLOWED=$(printf '%s' "$ONE" | jq -r '.status.disruptionsAllowed // 0')

    SELECTOR=$(printf '%s' "$ONE" | build_selector)
    [ "$SELECTOR" = "null" ] && SELECTOR=""

    TOTAL_PODS=0
    if [ -n "$SELECTOR" ]; then
      PODS_JSON=$(kubectl get po -n "$NS" -l "$SELECTOR" -o json 2>/dev/null || echo '{"items":[]}')
      TOTAL_PODS=$(printf '%s' "$PODS_JSON" | jq '[.items[]
        | select(.metadata.deletionTimestamp|not)
        | select(.status.phase=="Running")
        | select(any(.status.conditions[]?; .type=="Ready" and .status=="True"))
      ] | length')
    fi

    RISK=""
    if printf '%s' "$ONE" | has_unsupported_expr; then RISK="${RISK:+$RISK|}expr_unsupported"; fi
    [ -z "$SELECTOR" ] && RISK="${RISK:+$RISK|}selector_empty"
    [ "$ALLOWED" -eq 0 ] && RISK="${RISK:+$RISK|}allowed0"
    [ "$TOTAL_PODS" -eq 0 ] && RISK="${RISK:+$RISK|}orphan"
    if [ -n "${MIN_AVAIL:-}" ] && [ "$MIN_AVAIL" != "null" ] && [ "$MIN_AVAIL" -ge "${TOTAL_PODS:-0}" ] 2>/dev/null; then
      RISK="${RISK:+$RISK|}tight_min"
    fi
    if [ -n "${MAX_UNAVAIL:-}" ] && [ "$MAX_UNAVAIL" = "0" ]; then
      RISK="${RISK:+$RISK|}max0"
    fi
    if [ "$DESIRED_HEALTHY" -gt "${TOTAL_PODS:-0}" ] 2>/dev/null; then
      RISK="${RISK:+$RISK|}config_mismatch"
    fi
    [ -z "$RISK" ] && RISK="ok"
    [ "$RISK" != "ok" ] && TOTAL_RISK=$((TOTAL_RISK+1))

    RECO=$(recommend_for_flags "$NS" "$NAME" "$POLICY" "${MIN_AVAIL:-}" "${MAX_UNAVAIL:-}" "$ALLOWED" "${TOTAL_PODS:-0}" "$RISK")

    printf '%s,%s,%s,"%s",%s,%s,%s,%s,%s,"%s"\n' \
      "$NS" "$NAME" "$POLICY" "$SELECTOR" "$CURR_HEALTHY" "$DESIRED_HEALTHY" "$ALLOWED" "${TOTAL_PODS:-0}" "$RISK" "$RECO"

    idx=$((idx+1))
  done
done < "$NS_LIST_TMP"

if [ "$TOTAL_RISK" -gt 0 ]; then
  echo "# Summary: $TOTAL_RISK PDB flagged as risky" >&2
  exit 2
else
  echo "# Summary: No risky PDB found" >&2
  exit 0
fi
