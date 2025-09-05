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

def normalize_crlf(s: str) -> str:
    return s.replace("\r", "")

def strip_inline_comment(s: str) -> str:
    out=[]; in_s=False; in_d=False
    for ch in s:
        if ch=="'" and not in_d: in_s = not in_s
        elif ch=='"' and not in_s: in_d = not in_d
        if ch=="#" and not in_s and not in_d: break
        out.append(ch)
    return "".join(out)

def unquote(s: str) -> str:
    s=s.strip()
    if len(s)>=2 and ((s[0]==s[-1]=="'") or (s[0]==s[-1]=='"')): return s[1:-1]
    return s

def try_yaml(payload: str):
    """Trả về (annotations:dict, workloads:list[str]); không phụ thuộc PyYAML."""
    try:
        import yaml  # optional
        data = yaml.safe_load(payload) or {}
        annotations = data.get("annotations") or {}
        wl_val = data.get("workload-list") or ""
        wl_raw = [str(x) for x in wl_val] if isinstance(wl_val, list) else str(wl_val).splitlines()
        workloads=[]
        for line in wl_raw:
            line = strip_inline_comment(line).strip()
            if "|" in line: workloads.append(line)
        annotations = {str(k): "" if v is None else str(v) for k,v in annotations.items()}
        return annotations, workloads
    except Exception:
        # minimal parser
        lines = payload.splitlines()
        # annotations block
        anno_block=[]; in_anno=False
        for line in lines:
            stripped=line.lstrip(" \t")
            if re.match(r"^annotations:\s*(#.*)?$", stripped):
                in_anno=True; continue
            if in_anno:
                if line and line[0] not in (" ","\t"): in_anno=False
                else: anno_block.append(line)
        annotations={}
        for raw in anno_block:
            m=re.match(r"^[ \t]*([A-Za-z0-9_.\-]+):[ \t]*(.*)$", raw)
            if not m: continue
            k, v = m.group(1), strip_inline_comment(m.group(2)).strip()
            annotations[k]=unquote(v)

        # workload-list block
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
            w=strip_inline_comment(w).strip()
            if "|" in w: workloads.append(w)
        return annotations, workloads

def parse_date_loose(s: str):
    """Chấp nhận YYYYMMDD hoặc YYYY-MM-DD; trả về date hoặc None nếu vô hiệu."""
    if not s: return None
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def main():
    # input
    payload_file = sys.argv[1] if len(sys.argv)>1 else ""
    if payload_file and os.path.isfile(payload_file):
        payload = open(payload_file, "r", encoding="utf-8").read()
    else:
        payload = os.environ.get("EXC_PAYLOAD","")
        if not payload:
            print("❌ No input. Pass EXC_PAYLOAD or a file path.")
            sys.exit(1)
    payload = normalize_crlf(payload)

    annotations, workloads = try_yaml(payload)
    errs = []

    # 1) annotations block
    if not annotations:
        errs.append("Thiếu block `annotations:` ở YAML.")

    # 2) mode XOR
    ex247 = str(annotations.get("on-exeption-247","")).strip().lower() in ("true","1","yes","y","on")
    exow  = str(annotations.get("on-exeption-out-worktime","")).strip().lower() in ("true","1","yes","y","on")
    if not (ex247 ^ exow):
        errs.append("Trường mode không hợp lệ: yêu cầu **chỉ một** trong hai `on-exeption-247` hoặc `on-exeption-out-worktime` = true.")

    # 3) requester & reason
    requester = unquote((annotations.get("on-exeption-requester") or "").strip())
    reason    = unquote((annotations.get("on-exeption-reason") or "").strip())
    if not requester:
        errs.append("Thiếu `on-exeption-requester` (bắt buộc).")
    if not reason:
        errs.append("Thiếu `on-exeption-reason` (bắt buộc).")

    # 4) endtime
    end_raw = (annotations.get("on-exeption-endtime") or "").strip()
    if not end_raw:
        errs.append("Thiếu `on-exeption-endtime` (bắt buộc).")
        end_date = None
    else:
        end_date = parse_date_loose(end_raw)
        if end_date is None:
            errs.append(f"`on-exeption-endtime` không hợp lệ: '{end_raw}' (chỉ nhận YYYYMMDD hoặc YYYY-MM-DD, và phải là ngày hợp lệ).")

    # 5) policy window
    t = today_local()
    if end_date:
        if end_date < t:
            errs.append(f"`on-exeption-endtime` đã qua hạn: {end_date.isoformat()} < {t.isoformat()}.")
        elif (end_date - t).days > MAX_DAYS_ALLOWED:
            errs.append(f"`on-exeption-endtime` vượt quá {MAX_DAYS_ALLOWED} ngày cho phép (ngày: {end_date.isoformat()}, hôm nay: {t.isoformat()}).")

    # 6) workload-list
    if not workloads:
        errs.append("Thiếu `workload-list` (ít nhất 1 dòng).")
    else:
        bad=[]
        for w in workloads:
            parts = [p.strip() for p in w.split("|",1)]
            if len(parts)!=2 or not parts[0] or not parts[1]:
                bad.append(w)
        if bad:
            errs.append("Các dòng workload-list không hợp lệ (đúng dạng: `namespace | workload`):\n  - " + "\n  - ".join(bad))

    # verdict
    if errs:
        print("❌ Payload không hợp lệ. Chi tiết:")
        for i,e in enumerate(errs,1):
            print(f"  {i}. {e}")
        # gợi ý nhỏ
        print("\n🔎 Mẹo: kiểm tra lại block `annotations:` và `workload-list:` theo mẫu đã phổ biến.")
        sys.exit(2)

    # OK
    print("✅ Payload OK")
    print(f"   - Mode: {'24/7' if ex247 else 'Ngoài giờ'}")
    print(f"   - Requester: {requester}")
    print(f"   - Reason: {reason}")
    print(f"   - End date: {end_date.isoformat()} (<= {MAX_DAYS_ALLOWED} ngày)")
    print(f"   - Workloads: {len(workloads)} dòng hợp lệ")
    sys.exit(0)

if __name__ == "__main__":
    main()
