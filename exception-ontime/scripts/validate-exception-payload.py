#!/usr/bin/env python3
import os, sys, re, datetime

MAX_DAYS_ALLOWED = int(os.environ.get("MAX_DAYS_ALLOWED", "60"))
TZ_ENV           = os.environ.get("TZ", "Asia/Bangkok")
TODAY_OVERRIDE   = os.environ.get("TODAY", "").strip()

def tzset_if_possible():
    try:
        os.environ["TZ"] = TZ_ENV
        import time as _time
        if hasattr(_time, "tzset"): _time.tzset()
    except Exception:
        pass

def today_local() -> datetime.date:
    tzset_if_possible()
    if TODAY_OVERRIDE:
        try:
            return datetime.date.fromisoformat(TODAY_OVERRIDE)
        except Exception:
            pass
    return datetime.date.today()

def parse_date_loose(s: str):
    """Chấp nhận YYYY-MM-DD hoặc YYYYMMDD; trả về date hoặc None nếu vô hiệu."""
    if not s: return None
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("true","1","yes","y","on")

def main():
    errs = []

    # 1) đọc env
    ex247 = as_bool(os.environ.get("EXEC_ON_247", "false"))
    exout = as_bool(os.environ.get("EXEC_ON_OUT", "false"))
    requester = (os.environ.get("EXEC_REQUESTER") or "").strip()
    reason    = (os.environ.get("EXEC_REASON") or "").strip()
    end_raw   = (os.environ.get("EXEC_END_DATE") or "").strip()
    wl_text   = os.environ.get("EXEC_WORKLOAD_LIST") or ""

    # 2) validate mode
    # Đổi thành: ít nhất một flag bật
    if not (ex247 or exout):
        errs.append("Trường mode không hợp lệ: cần bật ít nhất một trong hai EXEC_ON_247 hoặc EXEC_ON_OUT.")

    # 3) requester & reason
    if not requester:
        errs.append("Thiếu EXEC_REQUESTER (bắt buộc).")
    if not reason:
        errs.append("Thiếu EXEC_REASON (bắt buộc).")

    # 4) end_date
    if not end_raw:
        errs.append("Thiếu EXEC_END_DATE (bắt buộc).")
        end_date = None
    else:
        end_date = parse_date_loose(end_raw)
        if end_date is None:
            errs.append(f"EXEC_END_DATE không hợp lệ: '{end_raw}' (chỉ nhận YYYYMMDD hoặc YYYY-MM-DD, và phải là ngày hợp lệ).")

    # 5) policy window
    t = today_local()
    if end_date:
        if end_date < t:
            errs.append(f"EXEC_END_DATE đã qua hạn: {end_date.isoformat()} < {t.isoformat()}.")
        elif (end_date - t).days > MAX_DAYS_ALLOWED:
            errs.append(f"EXEC_END_DATE vượt quá {MAX_DAYS_ALLOWED} ngày cho phép (ngày: {end_date.isoformat()}, hôm nay: {t.isoformat()}).")

    # 6) workload list
    workloads = []
    for line in wl_text.splitlines():
        line = line.strip()
        if not line: continue
        parts = [p.strip() for p in line.split("|",1)]
        if len(parts)!=2 or not parts[0] or not parts[1]:
            errs.append(f"Dòng workload-list không hợp lệ (đúng dạng: `namespace | workload`): {line}")
        else:
            workloads.append(line)
    if not workloads:
        errs.append("Thiếu EXEC_WORKLOAD_LIST (ít nhất 1 dòng).")

    # verdict
    if errs:
        print("❌ Tham số không hợp lệ. Chi tiết:")
        for i,e in enumerate(errs,1):
            print(f"  {i}. {e}")
        sys.exit(2)

    # OK
    print("✅ Parameters OK")
    print(f"   - Mode: {'24/7' if ex247 else ''}{'Ngoài giờ' if exout else ''}")
    print(f"   - Requester: {requester}")
    print(f"   - Reason: {reason}")
    print(f"   - End date: {end_date.isoformat()} (<= {MAX_DAYS_ALLOWED} ngày)")
    print(f"   - Workloads: {len(workloads)} dòng hợp lệ")
    sys.exit(0)

if __name__ == "__main__":
    main()
