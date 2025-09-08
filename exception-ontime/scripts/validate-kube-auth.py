#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, re, subprocess, shlex

# ===== Flags / Env =====
DEBUG            = os.environ.get("DEBUG", "0").lower() in ("1","true","yes")
STRICT_PATCH     = os.environ.get("STRICT_PATCH", "0").lower() in ("1","true","yes")
ALLOW_UNKNOWN_NS = os.environ.get("ALLOW_UNKNOWN_NS", "0").lower() in ("1","true","yes")

def dbg(msg):
    if DEBUG:
        print(msg, flush=True)

# ===== Input parsing (ENV-first; strict format for EXEC_WORKLOAD_LIST) =====
def normalize_crlf(s: str) -> str:
    return s.replace("\r", "") if s else s

def strip_inline_comment(s: str) -> str:
    """Remove inline comments outside quotes."""
    out=[]; in_s=False; in_d=False
    for ch in s:
        if ch=="'" and not in_d: in_s = not in_s
        elif ch=='"' and not in_s: in_d = not in_d
        if ch=="#" and not in_s and not in_d: break
        out.append(ch)
    return "".join(out)

def parse_exec_ns_list(s: str):
    """EXEC_NS_LIST: split by comma/space/newline; dedupe & sort."""
    if not s: return []
    parts = re.split(r"[,\s]+", s.strip())
    return sorted({p for p in parts if p})

def parse_exec_workload_list_strict(block: str):
    """
    EXEC_WORKLOAD_LIST (STRICT):
      - Mỗi dòng bắt buộc có '|' (có thể có khoảng trắng quanh '|')
      - Vế trái = namespace (không rỗng)
      - Vế phải = workload name (không validate sâu ở đây)
    Trả về: (namespaces_sorted, invalid_lines)
    invalid_lines: list[(line_no, line_content, reason)]
    """
    ns_set=set()
    invalid=[]
    if not block:
        return [], invalid
    for idx, raw in enumerate(normalize_crlf(block).splitlines(), start=1):
        line_display = raw.rstrip("\n")
        line = strip_inline_comment(raw).strip()
        if not line:
            continue  # bỏ qua dòng trống/comment
        if "|" not in line:
            invalid.append((idx, line_display, "missing '|' separator"))
            continue
        left, right = line.split("|", 1)
        ns = left.strip()
        if not ns:
            invalid.append((idx, line_display, "empty namespace (left side)"))
            continue
        # workload name có thể bỏ trống về mặt RBAC; không ép ở đây
        ns_set.add(ns)
    return sorted(ns_set), invalid

def read_lines_file(path: str):
    if not path: return []
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File không tồn tại: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

def collect_namespaces():
    """
    Priority:
      1) EXEC_NS_LIST
      2) EXEC_WORKLOAD_LIST (STRICT)
      3) MANAGED_NS_FILE
    """
    # 1) EXEC_NS_LIST (tự do)
    exec_ns_list = os.environ.get("EXEC_NS_LIST", "")
    ns_from_list = parse_exec_ns_list(exec_ns_list)
    if ns_from_list:
        return ns_from_list

    # 2) EXEC_WORKLOAD_LIST (STRICT)
    wl = os.environ.get("EXEC_WORKLOAD_LIST", "")
    ns_from_wl, invalid = parse_exec_workload_list_strict(wl)
    if invalid:
        print("❌ EXEC_WORKLOAD_LIST sai format (yêu cầu: `namespace | workloadName`). Lỗi chi tiết:")
        for ln, content, why in invalid:
            print(f"  - line {ln}: {why}  ==> `{content}`")
        print("\nVí dụ hợp lệ:")
        print("  sb-check   | multitool")
        print("  sb-backend | workloadA\n")
        sys.exit(1)
    if ns_from_wl:
        return ns_from_wl

    # 3) MANAGED_NS_FILE
    managed_file = os.environ.get("MANAGED_NS_FILE", "")
    if managed_file:
        try:
            return sorted(set(read_lines_file(managed_file)))
        except FileNotFoundError as e:
            print(f"❌ {e}")
            sys.exit(2)

    return []

# ===== kubectl helpers =====
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
    Return (status, detail)
    status ∈ {'exists','not_found','unknown'}
    """
    rc, out, err = run_kubectl(kcfg, ctx, ["get","ns", ns, "-o","name"], timeout=10)
    msg = (out + "\n" + err).lower()
    if rc == 0:
        return "exists", "ok"
    if "not found" in msg:
        return "not_found", "get ns -> not found"
    if "forbidden" in msg or "permission" in msg or "unauthorized" in msg:
        # Disambiguate with a resource call
        rc2, out2, err2 = run_kubectl(kcfg, ctx, ["get","pods","-n", ns], timeout=10)
        msg2 = (out2 + "\n" + err2).lower()
        if "namespaces" in msg2 and "not found" in msg2:
            return "not_found", "get pods -> namespaces not found"
        if rc2 == 0 or "forbidden" in msg2 or "permission" in msg2 or "unauthorized" in msg2:
            return "exists", "cannot read ns, but resource call suggests exists/forbidden"
        return "unknown", f"forbidden get ns, and ambiguous pods rc={rc2}"
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

# ===== main =====
def main():
    # kubeconfig
    kcfg = os.environ.get("KUBECONFIG_FILE") or os.environ.get("USER_KUBECONFIG") or os.environ.get("KUBECONFIG")
    if not kcfg or not os.path.isfile(kcfg) or os.path.getsize(kcfg)==0:
        print("❌ KUBECONFIG_FILE/USER_KUBECONFIG không hợp lệ (thiếu hoặc trống).")
        sys.exit(2)

    ctx = (os.environ.get("KUBE_CONTEXT","") or "").strip()
    if not ctx:
        ctx = current_context(kcfg)
    dbg(f"Using context: {ctx or '(current-context)'}")

    # quick connectivity
    rc, out, err = run_kubectl(kcfg, ctx, ["version","--short"], timeout=10)
    if rc != 0:
        print("❌ Không kết nối được cluster bằng kubeconfig đã cung cấp.")
        if DEBUG: print(err)
        sys.exit(5)

    # namespaces from ENV / file (strict)
    namespaces = collect_namespaces()
    if not namespaces:
        print("❌ Không xác định được namespace để kiểm tra RBAC.")
        print("   Hãy set một trong các biến sau:")
        print("   - EXEC_NS_LIST='ns-a,ns-b' (hoặc phân tách bằng khoảng trắng/ xuống dòng)")
        print("   - EXEC_WORKLOAD_LIST='ns-a | app1\\nns-b | app2'  (BẮT BUỘC có dấu '|')")
        print("   - MANAGED_NS_FILE='/path/to/namespaces.txt' (mỗi dòng một namespace)")
        sys.exit(1)

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

    print("✅ RBAC OK cho các namespace:")
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
