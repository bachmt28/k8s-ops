#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, json, subprocess, shlex, time, datetime, random, fcntl, re
from typing import Dict, List, Tuple

# -------- Config --------
OUT_DIR        = os.environ.get("OUT_DIR", "/data/exceptions/out")
STATE_ROOT     = os.environ.get("STATE_ROOT", "/data/exceptions/state")
STATE_FILE     = os.path.join(STATE_ROOT, "replicas.json")
TZ             = os.environ.get("TZ", "Asia/Bangkok")

MANAGED_NS_FILE= os.environ.get("MANAGED_NS_FILE", "managed-ns.txt")   # regex per line
DENY_NS_FILE   = os.environ.get("DENY_NS_FILE", "deny-ns.txt")         # optional regex per line
HOLIDAYS_FILE  = os.environ.get("HOLIDAYS_FILE", "holidays.txt")
HOLIDAY_MODE   = os.environ.get("HOLIDAY_MODE", "hard_off").lower()    # hard_off only (per spec)

ACTION         = os.environ.get("ACTION", "auto").lower()
TARGET_DOWN    = int(os.environ.get("TARGET_DOWN", "0"))
DEFAULT_UP     = int(os.environ.get("DEFAULT_UP", "1"))
DOWN_HPA_HANDLING = os.environ.get("DOWN_HPA_HANDLING", "force").lower()  # skip | force

JITTER_MAX_S   = int(os.environ.get("JITTER_MAX_S", "120"))
HYST_MIN       = int(os.environ.get("HYST_MIN", "2"))     # minutes near window edges

DEBUG          = os.environ.get("DEBUG","0").lower() in ("1","true","yes")

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

def is_business_window(dt: datetime.datetime) -> bool:
    wd = weekday_index(dt)
    return (0 <= wd <= 4) and between(dt, "08:00", "18:00")

def is_out_window(dt: datetime.datetime) -> bool:
    wd = weekday_index(dt)
    if 0 <= wd <= 4:
        return between(dt, "18:00", "22:00")
    else:
        return between(dt, "09:00", "20:00")

def is_weekend(dt: datetime.datetime) -> bool:
    wd = weekday_index(dt)
    return wd >= 5

def near_edge(dt: datetime.datetime) -> bool:
    """True if within ±HYST_MIN around the edges we care about."""
    edges = []
    if 0 <= weekday_index(dt) <= 4:
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
        try: self.f.flush(); os.fsync(self.f.fileno())
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
    cmd = ["kubectl"] + args
    if DEBUG: print("[kubectl]", " ".join(shlex.quote(x) for x in cmd))
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, encoding="utf-8")
    return cp.returncode, (cp.stdout or "").strip(), (cp.stderr or "").strip()

def list_namespaces() -> List[str]:
    rc,out,err = run_k(["get","ns","-o","json"])
    if rc != 0: raise RuntimeError(f"kubectl get ns failed: {err}")
    try:
        obj = json.loads(out)
        return sorted([i["metadata"]["name"] for i in obj.get("items",[])])
    except Exception as e:
        raise RuntimeError(f"parse ns json failed: {e}")

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
    try:
        obj=json.loads(out)
        for it in obj.get("items",[]):
            k=it.get("kind","").lower()
            kind="deploy" if k=="deployment" else "statefulset"
            name=it["metadata"]["name"]
            items.append((kind,name))
    except Exception as e:
        if DEBUG: print(f"[warn] parse workloads ns={ns}:", e)
    return items

def hpa_index(ns: str) -> Dict[Tuple[str,str], int]:
    """map (kind,name) -> minReplicas (default 1)."""
    rc,out,err = run_k(["-n", ns, "get", "hpa", "-o", "json"])
    if rc != 0:
        return {}
    res={}
    try:
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
    except Exception as e:
        if DEBUG: print(f"[warn] parse hpa ns={ns}:", e)
    return res

def get_replicas(ns: str, kind: str, name: str) -> int:
    rc,out,err = run_k(["-n", ns, "get", kind, name, "-o", "jsonpath={.spec.replicas}"])
    if rc != 0 or not out: return -1
    try: return int(out)
    except: return -1

def scale_to(ns: str, kind: str, name: str, replicas: int) -> bool:
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

def exception_mode_for(ns: str, name: str, active_map: Dict[str,dict]) -> str:
    """Return 'none'|'out_worktime'|'247' for (ns,name) with __ALL__ override."""
    all_key=f"{ns}|__ALL__"
    if all_key in active_map:
        return active_map[all_key]["mode"]
    key=f"{ns}|{name}"
    if key in active_map:
        return active_map[key]["mode"]
    return "none"

# -------- Decisions --------
def should_up_in_weekday_prestart() -> bool:
    # Always up everything in managed ns (not holiday handled outside)
    return True

def should_up_in_weekend_pre(mode: str) -> bool:
    # Only exceptions should be up
    return mode in ("out_worktime","247")

def should_up_in_enter_out(mode: str) -> bool:
    # Only exceptions should be up
    return mode in ("out_worktime","247")

def should_keep_up_247(mode: str) -> bool:
    return mode == "247"

# -------- Main --------
def main():
    now = local_now()
    today = today_iso()
    is_holiday = (today in load_holidays())
    active = load_active_map()
    state = load_state()

    # Resolve managed namespaces
    try:
        mns = get_managed_namespaces()
    except Exception as e:
        print(f"❌ managed namespaces error: {e}")
        sys.exit(2)

    print(f"⏱️  now={now} TZ={TZ} action={ACTION} holiday={is_holiday} managed_ns={len(mns)}")

    # Holiday hard_off: scale down all
    if is_holiday and HOLIDAY_MODE == "hard_off":
        print("🎌 Holiday hard_off → scale DOWN all workloads in managed namespaces.")
        changed = 0
        for ns in mns:
            hpa = hpa_index(ns)
            for kind,name in list_workloads(ns):
                cur = get_replicas(ns, kind, name)
                if cur < 0: 
                    print(f"⚠️  cannot get replicas for {kind}/{name} -n {ns}")
                    continue
                # handle HPA down
                if (kind,name) in hpa and DOWN_HPA_HANDLING == "skip":
                    print(f"↪️  skip down {kind}/{name} -n {ns} (HPA min={hpa[(kind,name)]})")
                    continue
                if cur > TARGET_DOWN:
                    state[f"{ns}|{kind}|{name}"] = {"prev_replicas": cur, "last_down": time.time()}
                    if scale_to(ns, kind, name, TARGET_DOWN):
                        changed += 1
        save_state(state)
        print(f"✅ Done (holiday). changed={changed}")
        sys.exit(0)

    # Decide default action if auto
    act = ACTION
    if act == "auto":
        if is_weekend(now):
            if near_edge(now) and between(now,"08:45","09:05"):
                act = "weekend_pre"
            elif is_out_window(now) and near_edge(now) and between(now,"19:55","20:05"):
                act = "weekend_close"
            else:
                # safe no-op
                act = "noop"
        else:
            if near_edge(now) and between(now,"07:10","08:05"):
                act = "weekday_prestart"
            elif near_edge(now) and between(now,"17:55","18:05"):
                act = "weekday_enter_out"
            else:
                act = "noop"

    print(f"▶️  resolved action: {act}")

    changed = 0
    for ns in mns:
        hpa = hpa_index(ns)
        for kind,name in list_workloads(ns):
            mode = exception_mode_for(ns, name, active)
            cur  = get_replicas(ns, kind, name)
            if cur < 0:
                print(f"⚠️  cannot get replicas for {kind}/{name} -n {ns}")
                continue

            # Decide desired behavior per action
            want_up = None
            if act == "weekday_prestart":
                want_up = should_up_in_weekday_prestart()
            elif act == "weekday_enter_out":
                want_up = should_up_in_enter_out(mode)
            elif act == "weekend_pre":
                want_up = should_up_in_weekend_pre(mode)
            elif act == "weekend_close":
                # keep 24/7 only
                want_up = should_keep_up_247(mode)
            elif act == "noop":
                continue
            else:
                continue

            # Compute target replicas if up
            if want_up:
                # When turning up
                if (kind,name) in hpa:
                    target = max(1, int(hpa[(kind,name)]))
                else:
                    prev = state.get(f"{ns}|{kind}|{name}",{}).get("prev_replicas", None)
                    target = int(prev) if isinstance(prev,int) and prev>=1 else DEFAULT_UP

                if cur == 0 and target >= 1:
                    # jitter
                    time.sleep(random.uniform(0, JITTER_MAX_S))
                    if scale_to(ns, kind, name, target):
                        state[f"{ns}|{kind}|{name}"] = {"prev_replicas": target, "last_up": time.time()}
                        changed += 1
                else:
                    # nothing to do; optionally refresh state
                    pass
            else:
                # Turning down
                if (kind,name) in hpa and DOWN_HPA_HANDLING == "skip":
                    print(f"↪️  skip down {kind}/{name} -n {ns} (HPA min={hpa[(kind,name)]})")
                    continue

                if cur > TARGET_DOWN:
                    # cache prev
                    state[f"{ns}|{kind}|{name}"] = {"prev_replicas": cur, "last_down": time.time()}
                    # jitter
                    time.sleep(random.uniform(0, JITTER_MAX_S))
                    if scale_to(ns, kind, name, TARGET_DOWN):
                        changed += 1

    save_state(state)
    print(f"✅ Done. changed={changed}")
    sys.exit(0)

if __name__ == "__main__":
    main()
