#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
    return s.replace("\r", "") if s else s

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

# ---------- Strict workload parser ----------
def parse_exec_workload_list_strict(block: str) -> Tuple[List[str], List[Tuple[int, str, str]]]:
    """
    EXEC_WORKLOAD_LIST (STRICT):
      - Mỗi dòng bắt buộc có '|'
      - Cho phép khoảng trắng quanh '|'
      - Vế trái = namespace (không rỗng)
      - Vế phải = workload name (không rỗng) -> nếu rỗng coi là lỗi (để tránh lệch format)
    Trả về: (list 'ns|workload' sạch), invalid_lines[(line_no, original, reason)]
    """
    cleaned = []
    invalid = []
    if not block:
        return [], invalid

    for idx, raw in enumerate(normalize_crlf(block).splitlines(), start=1):
        display = raw.rstrip("\n")
        line = strip_inline_comment(raw).strip()
        if not line:
            continue  # bỏ dòng trống/comment
        if "|" not in line:
            invalid.append((idx, display, "missing '|' separator"))
            continue
        left, right = line.split("|", 1)
        ns = left.strip()
        wl = right.strip()
        if not ns:
            invalid.append((idx, display, "empty namespace (left side)"))
            continue
        if not wl:
            invalid.append((idx, display, "empty workload (right side)"))
            continue
        cleaned.append(f"{ns}|{wl}")
    return cleaned, invalid

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
    # === ENV & required vars ===
    RAW_ROOT = os.environ.get("RAW_ROOT", "/data/exceptions/raw")
    RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "90"))
    RETENTION_DRY_RUN = os.environ.get("RETENTION_DRY_RUN", "0").lower() in ("1", "true", "yes")

    EXEC_ON_247   = os.environ.get("EXEC_ON_247", "false")
    EXEC_ON_OUT   = os.environ.get("EXEC_ON_OUT", "true")
    EXEC_REQUESTER = (os.environ.get("EXEC_REQUESTER") or "").strip()
    EXEC_REASON    = (os.environ.get("EXEC_REASON") or "").strip()
    EXEC_END_DATE  = (os.environ.get("EXEC_END_DATE") or "").strip()
    EXEC_WORKLOAD_LIST = os.environ.get("EXEC_WORKLOAD_LIST", "")

    # Thiếu biến bắt buộc -> fail sớm (đã được Preflight/Validator kiểm trước, nhưng vẫn siết ở đây)
    missing = []
    if not EXEC_REQUESTER: missing.append("EXEC_REQUESTER")
    if not EXEC_REASON:    missing.append("EXEC_REASON")
    if not EXEC_END_DATE:  missing.append("EXEC_END_DATE")
    if not EXEC_WORKLOAD_LIST: missing.append("EXEC_WORKLOAD_LIST")
    if missing:
        print("❌ Thiếu biến ENV bắt buộc: " + ", ".join(missing))
        sys.exit(1)

    # Chuẩn hoá ngày
    end_input = EXEC_END_DATE
    end_date  = norm_date(end_input)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_date):
        print(f"❌ EXEC_END_DATE sai định dạng (chấp nhận YYYYMMDD hoặc YYYY-MM-DD): {EXEC_END_DATE}")
        sys.exit(1)

    # Parse workload list (STRICT)
    wl_lines, invalid = parse_exec_workload_list_strict(EXEC_WORKLOAD_LIST)
    if invalid:
        print("❌ EXEC_WORKLOAD_LIST sai format (yêu cầu mỗi dòng: `namespace | workloadName`). Lỗi chi tiết:")
        for ln, content, why in invalid:
            print(f"  - line {ln}: {why}  ==> `{content}`")
        print("\nVí dụ hợp lệ:")
        print("  app-check   | multitool")
        print("  app-backend | workloadA\n")
        sys.exit(1)

    # Metadata Jenkins
    build_user   = os.environ.get("BUILD_USER_ID") or os.environ.get("BUILD_USER") or "unknown"
    job_name     = os.environ.get("JOB_NAME", "")
    build_url    = os.environ.get("BUILD_URL", "")
    build_number = os.environ.get("BUILD_NUMBER", "local")

    rid = req_id()
    created_at = now_utc_iso()

    # Chuẩn bị output workspace
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
        for raw in wl_lines:
            ns, wl = [p.strip() for p in raw.split("|", 1)]
            # Siết thêm một lớp an toàn (không thừa)
            if not ns or not wl:
                continue
            seq += 1
            ex247 = boolnorm(EXEC_ON_247)
            exow  = boolnorm(EXEC_ON_OUT)
            h = sha256_hex(f"{ns}|{wl}|{end_date}|{ex247}|{exow}|{EXEC_REQUESTER}|{EXEC_REASON}")

            rec = {
                "req_id": rid, "seq": seq, "ns": ns, "workload": wl,
                "on_exeption_247": ex247, "on_exeption_out_worktime": exow,
                "requester": EXEC_REQUESTER, "reason": EXEC_REASON,
                "end_date": end_date, "end_input": end_input,
                "created_at": created_at, "created_by": build_user,
                "source_job": job_name, "source_build": build_url,
                "status": "draft", "hash": h
            }
            fj.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cw.writerow([
                rid, seq, ns, wl, str(ex247).lower(), str(exow).lower(),
                EXEC_REQUESTER, EXEC_REASON, end_date, end_input, created_at, build_user,
                job_name, build_url, "draft", h
            ])

    dbg("✅ Draft created in workspace:")
    dbg(f" - {out_jsonl}")
    dbg(f" - {out_csv}")

    # Retention
    if safe_path_guard(RAW_ROOT):
        retention_cleanup(RAW_ROOT, RETAIN_DAYS, RETENTION_DRY_RUN)
    else:
        dbg("⚠️  Bỏ qua retention: RAW_ROOT không an toàn.")

    # Publish
    day_dir = os.path.join(RAW_ROOT, datetime.date.today().isoformat())
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
    try:
        print("\n=== Nội dung dữ liệu đã được ghi nhận ===\n")

        with open(out_raw_csv, "r", encoding="utf-8") as f:
            for line in f:
                print(line.rstrip())
    except Exception as e:
        print(f"⚠️  Không đọc được {out_raw_csv}: {e}")
if __name__ == "__main__":
    main()
