#!/usr/bin/env python3
import os, sys, re, json, csv, hashlib, time, glob, shutil, datetime, random
from typing import Tuple, Dict, List

# ---------- DEBUG ----------
DEBUG = os.environ.get("DEBUG", "0").lower() in ("1", "true", "yes")

def dbg(msg: str):
    if DEBUG:
        print(msg, flush=True)

# ---------- Utils ----------
def now_utc_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def req_id() -> str:
    rand = "".join(random.choice("0123456789abcdef") for _ in range(4))
    return f"exc-{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{rand}"

def normalize_crlf(s: str) -> str:
    return s.replace("\r", "")

def strip_inline_comment(s: str) -> str:
    """Remove trailing # comment outside quotes."""
    out = []
    in_s = in_d = False
    for ch in s:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        if ch == "#" and not in_s and not in_d:
            break
        out.append(ch)
    return "".join(out)

def unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == "'") or (s[0] == s[-1] == '"')):
        return s[1:-1]
    return s

def boolnorm(v: str) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")

def norm_date(s: str) -> str:
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):        # YYYYMMDD -> YYYY-MM-DD
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

# ---------- Parsing ----------
def parse_payload_minimal(payload: str) -> Tuple[Dict[str, str], List[str]]:
    """Parse top-level `annotations:` and indented `workload-list:` (no YAML deps)."""
    lines = payload.splitlines()

    # annotations
    annotations_block: List[str] = []
    in_anno = False
    for line in lines:
        stripped = line.lstrip(" \t")
        if re.match(r"^annotations:\s*(#.*)?$", stripped):
            in_anno = True
            continue
        if in_anno:
            if line and line[0] not in (" ", "\t"):
                in_anno = False
            else:
                annotations_block.append(line)

    ann: Dict[str, str] = {}
    for raw in annotations_block:
        m = re.match(r"^[ \t]*([A-Za-z0-9_.\-]+):[ \t]*(.*)$", raw)
        if not m:
            continue
        k, v = m.group(1), strip_inline_comment(m.group(2)).strip()
        ann[k] = unquote(v)

    # workload-list
    work_block: List[str] = []
    in_work = False
    for line in lines:
        stripped = line.lstrip(" \t")
        if re.match(r"^workload-list:\s*(\|?-?)\s*$", stripped):
            in_work = True
            continue
        if in_work:
            if line and line[0] not in (" ", "\t"):
                in_work = False
            else:
                work_block.append(line)

    workloads: List[str] = []
    for wline in work_block:
        wline = strip_inline_comment(wline).strip()
        if "|" in wline:
            workloads.append(wline)

    return ann, workloads

def try_yaml(payload: str) -> Tuple[Dict[str, str], List[str]]:
    """Try PyYAML if available; else minimal parser."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(payload) or {}
        annotations = data.get("annotations") or {}
        wl_val = data.get("workload-list") or ""
        if isinstance(wl_val, list):
            wl_raw = [str(x) for x in wl_val]
        else:
            wl_raw = str(wl_val).splitlines()
        workloads: List[str] = []
        for line in wl_raw:
            line = strip_inline_comment(line).strip()
            if "|" in line:
                workloads.append(line)
        annotations = {str(k): ("" if v is None else str(v)) for k, v in annotations.items()}
        return annotations, workloads
    except Exception:
        return parse_payload_minimal(payload)

# ---------- Retention ----------
def safe_path_guard(raw_root: str) -> bool:
    if not raw_root or raw_root == "/":
        dbg(f"❌ RAW_ROOT nguy hiểm: '{raw_root}'")
        return False
    if "/exceptions/raw" not in raw_root:
        dbg(f"❌ RAW_ROOT không hợp lệ (y/c chứa '/exceptions/raw'): {raw_root}")
        return False
    ensure_dir(raw_root)
    return True

def retention_cleanup(raw_root: str, retain_days: int, dry_run: bool):
    dbg(f"🧹 Retention: path={raw_root}, keep {retain_days}d, dry_run={int(dry_run)}")
    lock_dir = os.path.join(raw_root, ".retention.lock")
    for _ in range(60):
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            time.sleep(1)
    else:
        dbg("⚠️  Retention lock timeout, skip")
        return
    try:
        cutoff = time.time() - retain_days * 86400
        patterns = ("raw-*.jsonl", "raw-*.csv", "raw-*.meta")
        victims = []
        for root, _, _ in os.walk(raw_root):
            for pat in patterns:
                for p in glob.glob(os.path.join(root, pat)):
                    try:
                        if os.path.getmtime(p) < cutoff:
                            victims.append(p)
                    except Exception:
                        pass
        if not victims:
            dbg("✅ Không có file quá hạn.")
            return
        dbg(f"📄 File quá hạn ({len(victims)}):")
        for p in victims:
            dbg(f"  - {p}")
        if dry_run:
            dbg("🔎 DRY-RUN: chỉ liệt kê, KHÔNG xoá. (RETENTION_DRY_RUN=0 để xoá thật)")
        else:
            for p in victims:
                try:
                    os.remove(p)
                except Exception:
                    pass
            dbg(f"🗑️  Đã xoá {len(victims)} file.")
    finally:
        try:
            os.rmdir(lock_dir)
        except Exception:
            pass

# ---------- Main ----------
def main():
    # input
    payload_file = sys.argv[1] if len(sys.argv) > 1 else ""
    if payload_file and os.path.isfile(payload_file):
        with open(payload_file, "r", encoding="utf-8") as f:
            payload = f.read()
    else:
        payload = os.environ.get("EXC_PAYLOAD", "")
        if not payload:
            # im lặng khi thiếu input (validator chịu trách nhiệm fail-fast)
            sys.exit(1)
    payload = normalize_crlf(payload)

    raw_root = os.environ.get("RAW_ROOT", "/data/exceptions/raw")
    retain_days = int(os.environ.get("RETAIN_DAYS", "90"))
    retention_dry_run = os.environ.get("RETENTION_DRY_RUN", "0").lower() in ("1", "true", "yes")

    build_user = os.environ.get("BUILD_USER_ID") or os.environ.get("BUILD_USER") or "unknown"
    job_name = os.environ.get("JOB_NAME", "")
    build_url = os.environ.get("BUILD_URL", "")
    build_number = os.environ.get("BUILD_NUMBER", "local")

    rid = req_id()
    created_at = now_utc_iso()

    # parse
    annotations, workloads = try_yaml(payload)

    # fields
    ex247 = boolnorm(annotations.get("on-exeption-247", "false"))
    exow  = boolnorm(annotations.get("on-exeption-out-worktime", "false"))
    requester = unquote(annotations.get("on-exeption-requester", "").strip())
    reason    = unquote(annotations.get("on-exeption-reason", "").strip())
    end_input = annotations.get("on-exeption-endtime", "").strip()
    end_date  = norm_date(end_input)

    # outputs in workspace
    out_jsonl = "exceptions_draft.jsonl"
    out_csv   = "exceptions_draft.csv"
    with open(out_jsonl, "w", encoding="utf-8") as fj, \
         open(out_csv, "w", newline="", encoding="utf-8") as fc:

        cw = csv.writer(fc)
        cw.writerow([
            "req_id","seq","ns","workload","on_exeption_247","on_exeption_out_worktime",
            "requester","reason","end_date","end_input","created_at","created_by",
            "source_job","source_build","status","hash"
        ])

        seq = 0
        for raw in workloads:
            parts = raw.split("|", 1)  # "ns | workload"
            ns = parts[0].strip()
            wl = parts[1].strip() if len(parts) > 1 else ""
            if not ns or not wl:
                continue
            seq += 1
            h = sha256_hex(f"{ns}|{wl}|{end_date}|{ex247}|{exow}|{requester}|{reason}")

            rec = {
                "req_id": rid, "seq": seq, "ns": ns, "workload": wl,
                "on_exeption_247": ex247, "on_exeption_out_worktime": exow,
                "requester": requester, "reason": reason,
                "end_date": end_date, "end_input": end_input,
                "created_at": created_at, "created_by": build_user,
                "source_job": job_name, "source_build": build_url,
                "status": "draft", "hash": h
            }
            fj.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cw.writerow([
                rid, seq, ns, wl, str(ex247).lower(), str(exow).lower(),
                requester, reason, end_date, end_input, created_at, build_user,
                job_name, build_url, "draft", h
            ])

    dbg("✅ Draft created in workspace:")
    dbg(f" - {out_jsonl}")
    dbg(f" - {out_csv}")

    # retention
    if safe_path_guard(raw_root):
        retention_cleanup(raw_root, retain_days, retention_dry_run)
    else:
        dbg("⚠️  Bỏ qua retention: RAW_ROOT không an toàn.")

    # publish
    day_dir = os.path.join(raw_root, datetime.date.today().isoformat())
    ensure_dir(day_dir)

    out_raw_jsonl = os.path.join(day_dir, f"raw-{rid}-{build_number}.jsonl")
    out_raw_csv   = os.path.join(day_dir, f"raw-{rid}-{build_number}.csv")
    tmp_jsonl     = out_raw_jsonl + ".tmp"
    tmp_csv       = out_raw_csv + ".tmp"

    shutil.copyfile(out_jsonl, tmp_jsonl); os.replace(tmp_jsonl, out_raw_jsonl)
    shutil.copyfile(out_csv,   tmp_csv);   os.replace(tmp_csv,   out_raw_csv)

    meta_path = os.path.join(day_dir, f"raw-{rid}-{build_number}.meta")
    with open(meta_path, "w", encoding="utf-8") as fm:
        fm.write(f"created_at={created_at}\n")
        fm.write(f"created_by={build_user}\n")
        fm.write(f"job={job_name}\n")
        fm.write(f"build={build_url}\n")
        fm.write(f"files={os.path.basename(out_raw_jsonl)},{os.path.basename(out_raw_csv)}\n")

    dbg("📦 Published:")
    dbg(f" - {out_raw_jsonl}")
    dbg(f" - {out_raw_csv}")
    dbg(f" - {meta_path}")

if __name__ == "__main__":
    main()
