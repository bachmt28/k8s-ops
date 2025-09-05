#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, re, json, subprocess, shlex

DEBUG              = os.environ.get("DEBUG", "0").lower() in ("1","true","yes")
STRICT_PATCH       = os.environ.get("STRICT_PATCH", "0").lower() in ("1","true","yes")
ALLOW_UNKNOWN_NS   = os.environ.get("ALLOW_UNKNOWN_NS", "0").lower() in ("1","true","yes")

def dbg(msg):
    if DEBUG: print(msg, flush=True)

# ---- minimal payload parse: workload-list -> namespaces ----
def normalize_crlf(s: str) -> str: return s.replace("\r", "")

def strip_inline_comment(s: str) -> str:
    out=[]; in_s=False; in_d=False
    for ch in s:
        if ch=="'" and not in_d: in_s = not in_s
        elif ch=='"' and not in_s: in_d = not in_d
        if ch=="#" and not in_s and not in_d: break
        out.append(ch)
    return "".join(out)

def parse_payload_minimal(payload: str):
    lines = payload.splitlines()
    work_block=[]; in_work=False
    for line in lines:
        stripped=line.lstrip(" \t")
        if re.match(r"^workload-list:\s*(\|?-?)\s*$", stripped):
            in_work=True; continue
        if in_work:
            if line and line[0] not in (" ","\t"): in_work=False
            else: work_block.append(line)
    workloads=[]
    for w in work_block:
        w = strip_inline_comment(w).strip()
        if "|" in w: workloads.append(w)
    return workloads

def extract_namespaces(payload: str):
    wls = parse_payload_minimal(payload)
    ns_set=set()
    for line in wls:
        parts=[p.strip() for p in line.split("|",1)]
        if len(parts)==2 and parts[0]: ns_set.add(parts[0])
    return sorted(ns_set)

# ---- kubectl helpers ----
def run_kubectl(kcfg: str, ctx: str, args: list, timeout=15):
    base=["kubectl","--kubeconfig",kcfg]
    if ctx: base += ["--context", ctx]
    cmd = base + args
    dbg(f"[kubectl] {' '.join(shlex.quote(a) for a in cmd)}")
    try:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, encoding="utf-8")
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        return cp.returncode, out, err
    except FileNotFoundError:
        print("❌ kubectl không có trên agent. Cài đặt kubectl rồi chạy lại.")
        sys.exit(4)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"

def ns_exists(kcfg: str, ctx: str, ns: str):
    """
    Trả về (status, detail)
    status ∈ {'exists','not_found','unknown'}
    """
    rc, out, err = run_kubectl(kcfg, ctx, ["get","ns", ns, "-o","name"], timeout=10)
    msg = (out + "\n" + err).lower()
    if rc == 0:
        return "exists", "ok"
    if "not found" in msg:
        return "not_found", "get ns -> not found"
    if "forbidden" in msg or "permission" in msg or "unauthorized" in msg:
        # Fallback: thử gọi 1 resource trong ns để phân biệt not_found
        rc2, out2, err2 = run_kubectl(kcfg, ctx, ["get","pods","-n", ns], timeout=10)
        msg2 = (out2 + "\n" + err2).lower()
        if "namespaces" in msg2 and "not found" in msg2:
            return "not_found", "get pods -> namespaces not found"
        if rc2 == 0 or "forbidden" in msg2 or "permission" in msg2 or "unauthorized" in msg2:
            return "exists", "cannot read ns, but resource call suggests exists/forbidden"
        return "unknown", f"forbidden get ns, and ambiguous pods rc={rc2}"
    # Mọi lỗi khác (network, timeout, v.v.)
    return "unknown", f"get ns rc={rc} ({err[:120]})"

def can_i(kcfg: str, ctx: str, ns: str, verb: str, resource: str) -> bool:
    rc, out, err = run_kubectl(kcfg, ctx, ["auth","can-i",verb,resource,"-n",ns])
    if rc != 0:
        dbg(f"can-i error rc={rc}: {err}")
        return False
    return out.strip().lower() == "yes"

def current_context(kcfg: str) -> str:
    rc, out, err = run_kubectl(kcfg, "", ["config","current-context"])
    return out if rc==0 else ""

def main():
    # input payload
    payload_file = sys.argv[1] if len(sys.argv)>1 else ""
    if payload_file and os.path.isfile(payload_file):
        payload = open(payload_file,"r",encoding="utf-8").read()
    else:
        payload = os.environ.get("EXC_PAYLOAD","")
    if not payload:
        print("❌ Không có EXC_PAYLOAD để kiểm tra RBAC.")
        sys.exit(1)
    payload = normalize_crlf(payload)

    # kubeconfig
    kcfg = os.environ.get("KUBECONFIG_FILE") or os.environ.get("USER_KUBECONFIG") or os.environ.get("KUBECONFIG")
    if not kcfg or not os.path.isfile(kcfg) or os.path.getsize(kcfg)==0:
        print("❌ KUBECONFIG_FILE/USER_KUBECONFIG không hợp lệ (thiếu hoặc trống).")
        sys.exit(2)
    ctx = os.environ.get("KUBE_CONTEXT","").strip()
    if not ctx:
        ctx = current_context(kcfg)
    dbg(f"Using context: {ctx or '(current-context)'}")

    # quick connectivity
    rc, out, err = run_kubectl(kcfg, ctx, ["version","--short"], timeout=10)
    if rc != 0:
        print("❌ Không kết nối được cluster bằng kubeconfig đã cung cấp.")
        if DEBUG: print(err)
        sys.exit(5)

    # namespaces from payload
    namespaces = extract_namespaces(payload)
    if not namespaces:
        print("❌ Payload không chứa namespace nào trong `workload-list`.")
        sys.exit(2)

    failures = []
    results  = {}
    for ns in namespaces:
        status, detail = ns_exists(kcfg, ctx, ns)
        dbg(f"[ns:{ns}] existence={status} ({detail})")
        if status == "not_found":
            failures.append((ns, "namespace_not_found"))
            results[ns] = {"exists": False, "basic": False, "strict": (not STRICT_PATCH)}
            continue
        elif status == "unknown" and not ALLOW_UNKNOWN_NS:
            failures.append((ns, "namespace_unknown (set ALLOW_UNKNOWN_NS=1 to bypass)"))
            results[ns] = {"exists": None, "basic": False, "strict": (not STRICT_PATCH)}
            continue

        # RBAC checks
        basic_ok = (
            can_i(kcfg, ctx, ns, "list", "pods") or
            can_i(kcfg, ctx, ns, "get",  "deployments") or
            can_i(kcfg, ctx, ns, "get",  "statefulsets")
        )
        strict_ok = True
        if STRICT_PATCH:
            strict_ok = (
                can_i(kcfg, ctx, ns, "patch", "deployments/scale") or
                can_i(kcfg, ctx, ns, "patch", "statefulsets/scale")
            )

        results[ns] = {"exists": (status=="exists"), "basic": basic_ok, "strict": strict_ok}
        if not basic_ok or not strict_ok:
            reason = []
            if not basic_ok:  reason.append("no_basic_access(list pods | get deployments/statefulsets)")
            if not strict_ok: reason.append("no_patch_scale(deployments/statefulsets)")
            failures.append((ns, ", ".join(reason)))

    if failures:
        print("❌ Bạn KHÔNG có thẩm quyền / namespace không hợp lệ:")
        for ns, why in failures:
            print(f"  - {ns}: {why}")
        print("\n🔐 Yêu cầu:")
        if not ALLOW_UNKNOWN_NS:
            print("  - Namespace phải tồn tại; hoặc set ALLOW_UNKNOWN_NS=1 để bỏ qua kiểm tra tồn tại.")
        if STRICT_PATCH:
            print("  - Cần quyền patch scale trên deployments/statefulsets (hoặc tương đương).")
        print("  - Tối thiểu cần có quyền list pods hoặc get deployments/statefulsets trong namespace.\n")
        sys.exit(6)

    print("✅ RBAC OK cho tất cả namespace trong payload:")
    for ns in namespaces:
        info = results.get(ns, {})
        tag = []
        if info.get("exists") is True: tag.append("exists")
        elif info.get("exists") is None: tag.append("unknown-exists")
        if info.get("basic"):  tag.append("basic")
        if STRICT_PATCH and info.get("strict"): tag.append("patch-scale")
        print(f"  - {ns} ({', '.join(tag) or 'ok'})")

    sys.exit(0)

if __name__ == "__main__":
    main()
