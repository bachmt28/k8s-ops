#!/bin/bash
# Don PDB lam drain node treo vo han trong namespace app-*. Drain treo -> Machine xoa ket ->
# cluster-autoscaler dong bang node pool (da xay ra 29/09, 30/09 va 13/10/2026).
#
# Luat 1 - PDB "khong bao gio cho drain": doc TRANG THAI cua PDB, khong doan tu spec.
#   disruptionsAllowed = 0 trong khi co pod khoe va so pod khoe >= so pod mong doi, tuc la ca khi
#   moi pod deu khoe van khong evict duoc pod nao. Bat duoc moi dang: minAvailable >= so pod,
#   minAvailable 100% / theo %, maxUnavailable 0, khong dat ca hai (vu graphql-los 13/10/2026).
#   Khong dua vao spec + expectedPods: expectedPods ve 0 khi PDB khong doc duoc scale cua workload
#   (graphql-los: 0 trong khi co 1 pod khoe) hoac khi pod chua len (PDB vua apply) - luat cu bo sot.
#   PDB dang bao ve that (vd minAvailable 2, 3 replica, 1 pod dang hong) co pod khoe < mong doi:
#   khong dung toi. PDB khong chon pod nao (app scale 0) khong chan gi: khong dung toi.
# Luat 2 - nhieu PDB cung chon mot pod: eviction API tu choi han pod do ("This pod has more than one
#   PodDisruptionBudget, which the eviction subresource does not support"), du PDB nao cung con cho phep.
#   Hay gap khi hai ban deploy cung app (vd -uat va -uat-live) moi ban apply mot PDB chi chon label app.
#   Giu PDB tao som nhat, xoa PDB sau co pod trung. So bang label that cua pod nen bat ca selector
#   long nhau, khong chi selector giong het. PDB dung matchExpressions thi bo qua (chi log).
# Luat 3 - PDB thieu unhealthyPodEvictionPolicy: AlwaysAllow: va them truong do (khong xoa PDB). Mac dinh
#   IfHealthyBudget khong cho evict pod KHONG khoe (CrashLoop, chua Ready) khi PDB dang thieu pod khoe -
#   app 1 replica minAvailable 1 ma pod hong la drain treo mai. AlwaysAllow: pod hong luon evict duoc,
#   pod khoe van theo budget nhu cu. Chi va PDB tao tay: PDB co nhan managed-by (Helm) hoac ownerReferences
#   (operator) thi bo qua - va tay la lech voi chart/git, hoac bi operator ghi de; dat qua values cua chart.
# PDB dung chuan (maxUnavailable: 1, unhealthyPodEvictionPolicy: AlwaysAllow), khong trung pod, khong bi dung toi.
#
# Env:
#   DRY_RUN=true   chi liet ke, khong xoa (mac dinh false)
#   NS_REGEX       namespace xet (mac dinh ^sb-)
set -euo pipefail

DRY_RUN="${DRY_RUN:-false}"
NS_REGEX="${NS_REGEX:-^sb-}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

n=0
remove() {  # $1 ns, $2 ten, $3 ly do
  n=$((n + 1))
  if [ "$DRY_RUN" = "true" ]; then
    log "DRY_RUN $1/$2 ($3)"
  else
    kubectl -n "$1" delete pdb "$2" --wait=false >/dev/null
    log "XOA $1/$2 ($3)"
  fi
}

# --- Luat 1
list=$(kubectl get pdb -A --no-headers \
  -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,MIN:.spec.minAvailable,MAX:.spec.maxUnavailable,EXP:.status.expectedPods,HEALTHY:.status.currentHealthy,ALLOWED:.status.disruptionsAllowed \
  | awk -v re="$NS_REGEX" '
      $1 !~ re { next }
      {
        want = ($5 == "<none>" ? 0 : $5 + 0); healthy = ($6 == "<none>" ? 0 : $6 + 0)
        allowed = ($7 == "<none>" ? -1 : $7 + 0)
        if (allowed == 0 && healthy > 0 && healthy >= want)
          print $1, $2, "ALLOWED=0 khi " healthy "/" want " pod khoe, minAvailable=" $3 " maxUnavailable=" $4
      }')
if [ -n "$list" ]; then
  while read -r ns name why; do remove "$ns" "$name" "$why"; done <<< "$list"
fi

# --- Luat 2
# Dong PDB: "P ns thoi-diem-tao ten selector"; selector dang k=v,k=v, rong = selector {} chon moi pod,
# NONE = khong co selector (policy/v1: khong chon pod nao), EXPR = co matchExpressions.
# Dong pod: "O ns ten label" cung dang k=v,k=v. Luat 1 vua xoa thi PDB con deletionTimestamp, bo qua.
pdbs=$(kubectl get pdb -A -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}P {{.metadata.namespace}} {{.metadata.creationTimestamp}} {{.metadata.name}} {{if not .spec.selector}}NONE{{else if .spec.selector.matchExpressions}}EXPR{{else}}{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}{{end}}{{"\n"}}{{end}}{{end}}' \
  | awk -v re="$NS_REGEX" '$2 ~ re' | sort -k2,2 -k3,3)
# Chi namespace co tu 2 PDB moi co the trung. Lay pod tung namespace: list pod ca cluster mot lan
# (~1800 pod) lam kubectl OOM o limit 256Mi.
pods=""
for ns in $(awk '$5 != "NONE" { c[$2]++ } END { for (ns in c) if (c[ns] > 1) print ns }' <<< "$pdbs"); do
  pods+=$(kubectl -n "$ns" get pods -o go-template='{{range .items}}O {{.metadata.namespace}} {{.metadata.name}} {{range $k, $v := .metadata.labels}}{{$k}}={{$v}},{{end}}{{"\n"}}{{end}}')$'\n'
done
list=$(printf '%s\n%s\n' "$pods" "$pdbs" | awk '
  $1 == "O" { np[$2]++; pn[$2, np[$2]] = $3; pl[$2, np[$2]] = "," $4; next }
  $1 == "P" {
    ns = $2; name = $4; sel = $5
    if (sel == "NONE") next
    if (sel == "EXPR") { print ns, name, "BOQUA"; next }
    m = split(sel, kv, ",")
    hit = ""; clash = ""
    for (i = 1; i <= np[ns]; i++) {
      ok = 1
      for (j = 1; j <= m; j++) if (kv[j] != "" && index(pl[ns, i], "," kv[j] ",") == 0) { ok = 0; break }
      if (!ok) continue
      hit = hit " " i
      if ((ns, i) in owner && clash == "") clash = owner[ns, i] " (pod " pn[ns, i] ")"
    }
    if (clash != "") { print ns, name, "trung pod voi PDB " clash; next }
    k = split(hit, idx, " ")
    for (j = 1; j <= k; j++) owner[ns, idx[j]] = name
  }')
if [ -n "$list" ]; then
  while read -r ns name why; do
    if [ "$why" = "BOQUA" ]; then log "BO QUA $ns/$name (matchExpressions, khong so duoc)"; continue; fi
    remove "$ns" "$name" "$why"
  done <<< "$list"
fi

# --- Luat 3
m=0
list=$(kubectl get pdb -A --no-headers \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,POL:.spec.unhealthyPodEvictionPolicy,DEL:.metadata.deletionTimestamp,MGR:.metadata.labels.app\.kubernetes\.io/managed-by,OWNER:.metadata.ownerReferences[0].kind' \
  | awk -v re="$NS_REGEX" '$1 ~ re && $3 != "AlwaysAllow" && $4 == "<none>" && $5 == "<none>" && $6 == "<none>" {print $1, $2, $3}')
if [ -n "$list" ]; then
  while read -r ns name pol; do
    m=$((m + 1))
    if [ "$DRY_RUN" = "true" ]; then
      log "DRY_RUN VA $ns/$name (unhealthyPodEvictionPolicy=$pol -> AlwaysAllow)"
    else
      # PDB vua bi luat 1 xoa thi patch bao NotFound - bo qua, khong de set -e cat ngang ca luot
      if kubectl -n "$ns" patch pdb "$name" --type=merge -p '{"spec":{"unhealthyPodEvictionPolicy":"AlwaysAllow"}}' >/dev/null 2>&1; then
        log "VA $ns/$name (unhealthyPodEvictionPolicy=$pol -> AlwaysAllow)"
      else
        log "BO QUA VA $ns/$name (khong con hoac khong patch duoc)"
      fi
    fi
  done <<< "$list"
fi

log "xong: $n PDB xoa, $m PDB va AlwaysAllow (DRY_RUN=$DRY_RUN)"
