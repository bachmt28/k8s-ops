#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scale-by-exceptions.py

Jitter refactor:
- UP hàng loạt (weekday_prestart):       0..15s
- UP theo exception (weekend_pre, v.v.): 0..5s
- DOWN (enter_out, weekend_close, etc.): 0..2s

Các tính năng khác (như trước):
- ACTION=auto quyết định cửa sổ chạy theo giờ VN
- Holiday (HOLIDAY_MODE=hard_off): DOWN tất cả
- Ưu tiên exception: cụ thể vs ALL (ví dụ _ALL_) theo end_date (cụ thể > ALL nếu end_date muộn hơn)
- Hỗ trợ HPA minReplicas khi UP, lưu prev_replicas khi DOWN
- KUBECTL_TIMEOUT cho mọi lệnh kubectl
- MAX_ACTIONS_PER_RUN để cắt nhỏ batch mỗi tick

- Quyết định action sớm, nếu NOOP thì exit 0 (không gọi kubectl).
- Jitter:
  * Weekday prestart (UP hàng loạt):   0..15s
  * UP theo exception (weekend_pre...):0..5s
  * DOWN (enter_out, weekend_close...):0..2s
- Holiday hard_off: DOWN tất cả (bỏ qua NOOP).
"""

import os, sys, json, subprocess, shlex, time, datetime, random, fcntl, re
from typing import Dict, List, Tuple

# -------- Config (ENV) --------
OUT_DIR        = os.environ.get("OUT_DIR", "/data/exceptions/out")
STATE_ROOT     = os.environ.get("STATE_ROOT", "/data/exceptions/state")
STATE_FILE     = os.path.join(STATE_ROOT, "replicas.json")
TZ             = os.environ.get("TZ", "Asia/Bangkok")

MANAGED_NS_FILE= os.environ.get("MANAGED_NS_FILE", "managed-ns.txt")   # regex per line
DENY_NS_FILE   = os.environ.get("DENY_NS_FILE", "deny-ns.txt")         # optional regex per line
HOLIDAYS_FILE  = os.environ.get("HOLIDAYS_FILE", "holidays.txt")
HOLIDAY_MODE   = os.environ.get("HOLIDAY_MODE", "hard_off").lower()    # per spec

ACTION         = os.environ.get("ACTION", "auto").lower()
TARGET_DOWN    = int(os.environ.get("TARGET_DOWN", "0"))
DEFAULT_UP     = int(os.environ.get("DEFAULT_UP", "1"))
DOWN_HPA_HANDLING = os.environ.get("DOWN_HPA_HANDLING", "skip").lower()  # skip | force

# Jitter
_compat_j = os.environ.get("JITTER_MAX_S")
JITTER_UP_BULK_S   = int(os.environ.get("JITTER_UP_BULK_S", _compat_j or "5"))  # weekday_prestart
JITTER_UP_EXC_S    = int(os.environ.get("JITTER_UP_EXC_S", "2"))                 # weekend_pre / up theo exception
JITTER_DOWN_S      = int(os.environ.get("JITTER_DOWN_S", "1"))                   # mọi down
HYST_MIN           = int(os.environ.get("HYST_MIN", "3"))                        # phút ± cạnh mốc giờ

KUBECTL_TIMEOUT    = os.environ.get("KUBECTL_TIMEOUT", "10s")
MAX_ACTIONS_PER_RUN= int(os.environ.get("MAX_ACTIONS_PER_RUN", "0"))             # 0 = unlimited

DEBUG          = os.environ.get("DEBUG","0").lower() in ("1","true","yes")
DRY_RUN        = os.environ.get("DRY_RUN","0").lower() in ("1","true","yes")

# kube access (optional)
KCFG           = os.environ.get("KUBECONFIG_FILE") or os.environ.get("KUBECONFIG") or ""
KCTX           = os.environ.get("KUBE_CONTEXT","")

# -------- Time helpers --------
def local_now():
    try:
        os.environ["TZ"] = TZ
        time.tzset()
    except Exception:
        pass
    return datetime.datetime.now()

def weekday_index(dt: datetime.datetime) -> int:
    return dt.weekday()  # 0=Mon..6=Sun

def between(dt: datetime.datetime, start_hm: str, end_hm: str) -> bool:
    sh, sm = [int(x) for x in start_hm.split(":")]
    eh, em = [int(x) for x in end_hm.split(":")]
    s = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    e = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    return s <= dt <= e

def is_weekend(dt: datetime.datetime) -> bool:
    return weekday_index(dt) >= 5

def near_edge(dt: datetime.datetime) -> bool:
    """True nếu trong ±HYST_MIN phút quanh các mốc 08:00/18:00 (weekday) hoặc 09:00/20:00 (weekend)."""
    edges = []
    if not is_weekend(dt):
        edges += ["08:00","18:00"]
    else:
        edges += ["09:00","20:00"]
    for hm in edges:
        hh, mm = [int(x) for x in hm.split(":")]
        edge = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = abs((dt - edge).total_seconds()) / 60.0
        if delta <= HYST_MIN:
            return True
    return False

# -------- Files / state --------
class LockedFile:
    def __init__(self, path, mode="a+"):
        self.path=path; self.mode=mode; self.f=None
    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f=open(self.path, self.mode)
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self.f
    def __exit__(self, exc_type, exc, tb):
        try:
            self.f.flush(); os.fsync(self.f.fileno())
        except: pass
        try:
            fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            self.f.close()
        except: pass

def load_state() -> Dict[str, dict]:
    if not os.path.exists(STATE_FILE): return {}
    with LockedFile(STATE_FILE, "r+") as f:
        try:
            f.seek(0); return json.load(f) or {}
        except Exception:
            return {}

def save_state(data: dict):
    tmp = STATE_FILE + ".tmp"
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)

# -------- Kubectl helpers --------
def run_k(args: List[str], timeout=30) -> Tuple[int,str,str]:
    cmd = ["kubectl"]
    if KCFG:
        cmd += ["--kubeconfig", KCFG]
    if KCTX:
        cmd += ["--context", KCTX]
    cmd += ["--request-timeout", KUBECTL_TIMEOUT]
    cmd += args
    if DEBUG:
        print("[kubectl]", " ".join(shlex.quote(x) for x in cmd))
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, encoding="utf-8")
    return cp.returncode, (cp.stdout or "").strip(), (cp.stderr or "").strip()

def list_namespaces() -> List[str]:
    rc,out,err = run_k(["get","ns","-o","json"])
    if rc != 0: raise RuntimeError(f"kubectl get ns failed: {err}")
    obj = json.loads(out)
    return sorted([i["metadata"]["name"] for i in obj.get("items",[])])

def match_namespaces(all_ns: List[str], patterns: List[str], deny: List[str]) -> List[str]:
    res = []
    for ns in all_ns:
        if any(re.search(p, ns) for p in deny if p):
            continue
        if any(re.search(p, ns) for p in patterns if p):
            res.append(ns)
    return sorted(res)

def get_managed_namespaces() -> List[str]:
    pats = []
    if os.path.exists(MANAGED_NS_FILE):
        for line in open(MANAGED_NS_FILE, "r", encoding="utf-8"):
            s=line.strip()
            if not s or s.startswith("#"): continue
            pats.append(s)
    else:
        raise RuntimeError(f"Missing {MANAGED_NS_FILE}")

    deny=[]
    if os.path.exists(DENY_NS_FILE):
        for line in open(DENY_NS_FILE,"r",encoding="utf-8"):
            s=line.strip()
            if not s or s.startswith("#"): continue
            deny.append(s)

    return match_namespaces(list_namespaces(), pats, deny)

def list_workloads(ns: str) -> List[Tuple[str,str]]:
    """Return [(kind, name)] with kind in {'deploy','statefulset'}."""
    rc,out,err = run_k(["-n", ns, "get", "deploy,statefulset", "-o", "json"])
    if rc != 0:
        if DEBUG: print(f"[warn] get workloads ns={ns} failed:", err)
        return []
    items=[]
    obj=json.loads(out)
    for it in obj.get("items",[]):
        k=it.get("kind","").lower()
        kind="deploy" if k=="deployment" else "statefulset"
        name=it["metadata"]["name"]
        items.append((kind,name))
    return items

def hpa_index(ns: str) -> Dict[Tuple[str,str], int]:
    """map (kind,name) -> minReplicas (default 1)."""
    rc,out,err = run_k(["-n", ns, "get", "hpa", "-o", "json"])
    if rc != 0:
        return {}
    res={}
    obj=json.loads(out)
    for it in obj.get("items",[]):
        ref=it.get("spec",{}).get("scaleTargetRef",{})
        k=ref.get("kind","").lower()
        kind="deploy" if k=="deployment" else ("statefulset" if k=="statefulset" else None)
        name=ref.get("name","")
        if kind and name:
            m=it.get("spec",{}).get("minReplicas",1)
            try: m=int(m)
            except: m=1
            res[(kind,name)]=max(1,m)
    return res

def get_replicas(ns: str, kind: str, name: str) -> int:
    rc,out,err = run_k(["-n", ns, "get", kind, name, "-o", "jsonpath={.spec.replicas}"])
    if rc != 0 or not out: return -1
    try: return int(out)
    except: return -1

def scale_to(ns: str, kind: str, name: str, replicas: int) -> bool:
    if DRY_RUN:
        print(f"🧪 [dry-run] scale {kind}/{name} -n {ns} -> {replicas}")
        return True
    rc,out,err = run_k(["-n", ns, "scale", kind, name, f"--replicas={replicas}"])
    if rc == 0:
        print(f"✅ scaled {kind}/{name} -n {ns} -> {replicas}")
        return True
    else:
        print(f"❌ scale {kind}/{name} -n {ns} -> {replicas}: {err}")
        return False

# -------- Holidays & active exceptions --------
def load_holidays() -> set:
    s=set()
    if os.path.exists(HOLIDAYS_FILE):
        for line in open(HOLIDAYS_FILE,"r",encoding="utf-8"):
            t=line.strip()
            if not t or t.startswith("#"): continue
            s.add(t)
    return s

def today_iso():
    return local_now().date().isoformat()

def load_active_map() -> Dict[str, dict]:
    path = os.path.join(OUT_DIR, "active_exceptions.jsonl")
    m={}
    if not os.path.exists(path): return m
    with open(path,"r",encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            r=json.loads(line)
            ns=(r.get("ns") or "").strip()
            wl=(r.get("workload") or "").strip()
            mode=(r.get("mode") or "").strip()
            if not ns or not wl or mode not in ("247","out_worktime"): continue
            m[f"{ns}|{wl}"]=r
    return m

# --- precedence: specific vs ALL ---
def _parse_date_safe(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None

def exception_mode_for(ns: str, name: str, active_map: Dict[str,dict], today: datetime.date = None) -> str:
    """
    Trả về 'none' | 'out_worktime' | '247' theo thời điểm hiện tại:
      - Xét 2 nguồn: cụ thể (ns|name) và ALL (ns|_ALL_, ns|__ALL__, ns|ALL, ns|*).
      - Chỉ tính các record còn hiệu lực (end_date >= today).
      - Nếu 1 trong 2 nguồn đang hiệu lực có mode '247' → trả '247'.
      - Nếu không có '247' nhưng có 'out_worktime' → trả 'out_worktime'.
      - Nếu cả hai đã hết hạn → 'none'.
    => Đảm bảo: khi ALL(247) còn hiệu lực, workload cụ thể sẽ chạy 247; hết hạn ALL mới rơi về cụ thể/outtime.
    """
    if today is None:
        today = local_now().date()

    spec = active_map.get(f"{ns}|{name}")
    glob = (active_map.get(f"{ns}|_ALL_")
            or active_map.get(f"{ns}|__ALL__")
            or active_map.get(f"{ns}|ALL")
            or active_map.get(f"{ns}|*"))

    def active_mode(rec):
        if not rec:
            return None
        ed = _parse_date_safe(rec.get("end_date"))
        if not ed or ed < today:
            return None
        m = (rec.get("mode") or "").strip()
        return m if m in ("247","out_worktime") else None

    modes = [m for m in (active_mode(spec), active_mode(glob)) if m]

    if "247" in modes:
        return "247"
    if "out_worktime" in modes:
        return "out_worktime"
    return "none"


# -------- Decisions --------
def should_up_in_weekday_prestart() -> bool:
    # Weekday prestart: bật tất cả trong managed namespaces
    return True

def should_up_in_weekend_pre(mode: str) -> bool:
    # Weekend pre: chỉ bật exception (247 và ngoài giờ)
    return mode in ("out_worktime","247")

def should_up_in_enter_out(mode: str) -> bool:
    # 18:00 weekday: chỉ giữ exception
    return mode in ("out_worktime","247")

def should_keep_up_247(mode: str) -> bool:
    # 20:00 weekend: chỉ giữ 24/7
    return mode == "247"

# -------- Main --------
def main():
    now = local_now()
    today = today_iso()
    is_holiday = (today in load_holidays())

    # Resolve action early (no kubectl here)
    act = ACTION
    if act == "auto":
        if is_weekend(now):
            if between(now, "08:45", "09:05"):
                act = "weekend_pre"
            elif between(now, "19:55", "20:05"):
                act = "weekend_close"
            else:
                act = "noop"
        else:
            if between(now, "07:10", "08:05"):
                act = "weekday_prestart"
            elif between(now, "17:55", "18:05"):
                act = "weekday_enter_out"
            else:
                act = "noop"


    print(f"⏱️  now={now} TZ={TZ} action={act} holiday={is_holiday} DRY_RUN={int(DRY_RUN)}")

    # Fast exit when noop (except holiday hard_off)
    if act == "noop" and not (is_holiday and HOLIDAY_MODE == "hard_off"):
        print("🛌 NOOP window → fast exit (skip kubectl).")
        sys.exit(0)

    # From this point on, heavy stuff is allowed
    state = load_state()

    # Holiday hard_off: down all
    if is_holiday and HOLIDAY_MODE == "hard_off":
        print("🎌 Holiday hard_off → DOWN all workloads in managed namespaces.")
        try:
            mns = get_managed_namespaces()
        except Exception as e:
            print(f"❌ managed namespaces error: {e}")
            sys.exit(2)
        print(f"📦 managed namespaces: {len(mns)}")
        changed = 0
        actions = 0
        for ns in mns:
            for kind,name in list_workloads(ns):
                cur = get_replicas(ns, kind, name)
                if cur < 0:
                    print(f"⚠️  cannot get replicas for {kind}/{name} -n {ns}")
                    continue
                if cur > TARGET_DOWN:
                    state[f"{ns}|{kind}|{name}"] = {"prev_replicas": cur, "last_down": time.time()}
                    time.sleep(random.uniform(0, JITTER_DOWN_S))
                    if scale_to(ns, kind, name, TARGET_DOWN):
                        changed += 1
                        actions += 1
                        if MAX_ACTIONS_PER_RUN > 0 and actions >= MAX_ACTIONS_PER_RUN:
                            save_state(state)
                            print(f"⏳ Reached MAX_ACTIONS_PER_RUN={MAX_ACTIONS_PER_RUN}, partial done. changed={changed}")
                            sys.exit(0)
        save_state(state)
        print(f"✅ Done (holiday). changed={changed}")
        sys.exit(0)

    # Non-noop actions
    try:
        mns = get_managed_namespaces()
    except Exception as e:
        print(f"❌ managed namespaces error: {e}")
        sys.exit(2)

    print(f"📦 managed namespaces: {len(mns)}")
    if DEBUG:
        print(f"[DEBUG] JITTER_UP_BULK_S={JITTER_UP_BULK_S}, JITTER_UP_EXC_S={JITTER_UP_EXC_S}, JITTER_DOWN_S={JITTER_DOWN_S}, KUBECTL_TIMEOUT={KUBECTL_TIMEOUT}, MAX_ACTIONS_PER_RUN={MAX_ACTIONS_PER_RUN}")

    need_active = act in ("weekday_enter_out","weekend_pre","weekend_close")
    active = load_active_map() if need_active else {}

    changed = 0
    actions = 0
    for ns in mns:
        hpa = hpa_index(ns)
        for kind,name in list_workloads(ns):
            # choose up/down by act
            want_up = None
            mode = "none"
            if act == "weekday_prestart":
                want_up = should_up_in_weekday_prestart()           # bulk UP
            elif act == "weekday_enter_out":
                mode = exception_mode_for(ns, name, active)
                want_up = should_up_in_enter_out(mode)              # exception UP
            elif act == "weekend_pre":
                mode = exception_mode_for(ns, name, active)
                want_up = should_up_in_weekend_pre(mode)            # exception UP
            elif act == "weekend_close":
                mode = exception_mode_for(ns, name, active)
                want_up = should_keep_up_247(mode)                  # 24/7 UP
            else:
                continue

            cur = get_replicas(ns, kind, name)
            if cur < 0:
                print(f"⚠️  cannot get replicas for {kind}/{name} -n {ns}")
                continue

            if want_up:
                # compute target
                if (kind,name) in hpa:
                    target = max(1, int(hpa[(kind,name)]))
                else:
                    prev = state.get(f"{ns}|{kind}|{name}",{}).get("prev_replicas", None)
                    target = int(prev) if isinstance(prev,int) and prev>=1 else DEFAULT_UP

                if cur == 0 and target >= 1:
                    # jitter theo bối cảnh
                    if act == "weekday_prestart":
                        time.sleep(random.uniform(0, JITTER_UP_BULK_S))
                    else:
                        time.sleep(random.uniform(0, JITTER_UP_EXC_S))
                    if scale_to(ns, kind, name, target):
                        state[f"{ns}|{kind}|{name}"] = {"prev_replicas": target, "last_up": time.time()}
                        changed += 1
                        actions += 1
            else:
                # DOWN path
                # ⛔ weekend_pre: KHÔNG DOWN workload không thuộc exception
                if act == "weekend_pre":
                    continue

                force_down_this_time = (act in ("weekday_enter_out","weekend_close"))
                if (kind,name) in hpa and (not force_down_this_time) and DOWN_HPA_HANDLING == "skip":
                    print(f"↪️  skip down {kind}/{name} -n {ns} (HPA min={hpa[(kind,name)]})")
                    continue
                if cur > TARGET_DOWN:
                    state[f"{ns}|{kind}|{name}"] = {"prev_replicas": cur, "last_down": time.time()}
                    time.sleep(random.uniform(0, JITTER_DOWN_S))
                    if scale_to(ns, kind, name, TARGET_DOWN):
                        changed += 1
                        actions += 1

            if MAX_ACTIONS_PER_RUN > 0 and actions >= MAX_ACTIONS_PER_RUN:
                save_state(state)
                print(f"⏳ Reached MAX_ACTIONS_PER_RUN={MAX_ACTIONS_PER_RUN}, partial done. changed={changed}")
                sys.exit(0)

    save_state(state)
    print(f"✅ Done. changed={changed}")
    sys.exit(0)

if __name__ == "__main__":
    main()
