#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deduplicate exception registrations -> polished machine-friendly + human digest.

Inputs (env):
  RAW_ROOT        = /data/exceptions/raw
  OUT_DIR         = /data/exceptions/out
  LOOKBACK_DAYS   = 90
  MAX_DAYS        = 60
  TODAY           = YYYY-MM-DD (optional override)
  DEBUG           = 0/1
  DEBUG_DUMP_RAW  = 0/1
  DEBUG_DUMP_GROUPS = 0/1
  FILTER_NS       = only include namespace (exact match)
  FILTER_WL       = only include workload (exact match)

Outputs:
  polished_exceptions.jsonl / .csv
  invalid.jsonl
  digest_exceptions.csv
  digest_exceptions.webex.md
  digest_exceptions.html
"""
import os, sys, re, json, csv, datetime, time
from collections import defaultdict

# ---------- Config via env ----------
RAW_ROOT       = os.environ.get("RAW_ROOT", "/data/exceptions/raw")
OUT_DIR        = os.environ.get("OUT_DIR", "/data/exceptions/out")
LOOKBACK_DAYS  = int(os.environ.get("LOOKBACK_DAYS", "90"))
MAX_DAYS       = int(os.environ.get("MAX_DAYS", "60"))
TODAY_OVERRIDE = os.environ.get("TODAY", "").strip()  # e.g., 2025-09-05

DEBUG              = os.environ.get("DEBUG", "0").lower() in ("1","true","yes")
DEBUG_DUMP_RAW     = os.environ.get("DEBUG_DUMP_RAW", "0").lower() in ("1","true","yes")
DEBUG_DUMP_GROUPS  = os.environ.get("DEBUG_DUMP_GROUPS", "0").lower() in ("1","true","yes")
FILTER_NS          = os.environ.get("FILTER_NS", "").strip()
FILTER_WL          = os.environ.get("FILTER_WL", "").strip()

# ---------- Helpers ----------
def ensure_dir(p): os.makedirs(p, exist_ok=True)

def norm_date(s: str) -> str:
    s = (s or "").strip()
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else s

def parse_date(s: str):
    """Compat 3.6+: parse YYYY-MM-DD after norm_date"""
    s = norm_date(s or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime.datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None
    return None

def as_bool(v) -> bool:
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ("true","1","yes","y","on")

def get_today() -> datetime.date:
    if TODAY_OVERRIDE:
        try:
            return datetime.date.fromisoformat(TODAY_OVERRIDE)
        except Exception:
            pass
    return datetime.date.today()

def days_left(from_date: datetime.date, today: datetime.date) -> int:
    return (from_date - today).days

def discovered_raw_files(raw_root: str, lookback_days: int):
    cutoff = time.time() - lookback_days * 86400
    files = []
    for root, _, fs in os.walk(raw_root):
        for fn in fs:
            if fn.startswith("raw-") and fn.endswith(".jsonl"):
                path = os.path.join(root, fn)
                try:
                    if os.path.getmtime(path) >= cutoff:
                        files.append(path)
                except Exception:
                    files.append(path)
    return sorted(files)

def read_raw_lines(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            raw = line.rstrip("\n")
            line = raw.strip()
            if not line:
                continue
            try:
                yield i, raw, json.loads(line)
            except Exception:
                yield i, raw, {"_invalid": True, "_reason": "json_parse_error"}

def keep_rec(ns: str, wl: str) -> bool:
    if FILTER_NS and ns != FILTER_NS:
        return False
    if FILTER_WL and wl != FILTER_WL:
        return False
    return True

def mode_human(m: str) -> str:
    return "24/7" if m == "247" else "Ngoài giờ"

def esc_html(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# ---------- Main ----------
def main():
    ensure_dir(OUT_DIR)
    today = get_today()

    # lock to avoid concurrent overwrite
    lock_dir = os.path.join(OUT_DIR, ".lock")
    for _ in range(120):
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            time.sleep(1)
    else:
        print("⚠️  lock timeout; another run in progress?")
        sys.exit(0)

    try:
        if DEBUG:
            print(f"[DEBUG] RAW_ROOT={RAW_ROOT}")
            print(f"[DEBUG] OUT_DIR={OUT_DIR}")
            print(f"[DEBUG] LOOKBACK_DAYS={LOOKBACK_DAYS}, MAX_DAYS={MAX_DAYS}, TODAY={today.isoformat()}")
            if FILTER_NS or FILTER_WL:
                print(f"[DEBUG] FILTER_NS={FILTER_NS or '*'}, FILTER_WL={FILTER_WL or '*'}")

        raw_files = discovered_raw_files(RAW_ROOT, LOOKBACK_DAYS)
        if DEBUG:
            print(f"[DEBUG] Found {len(raw_files)} raw file(s):")
            for p in raw_files[:20]:
                print(f"        - {p}")

        groups = {}      # key -> aggregate (per ns|workload only, NO overlay)
        sources = defaultdict(list)   # key -> list
        invalid_records = []
        reason_counts = defaultdict(int)
        total_lines = 0
        parsed_ok = 0

        # --- pass 1: read raw & optionally dump each line
        for path in raw_files:
            if DEBUG_DUMP_RAW:
                print(f"\n[RAW] File: {path}")
            for ln, raw_line, r in read_raw_lines(path):
                total_lines += 1
                ns = (r.get("ns") or "").strip() if isinstance(r, dict) else ""
                wl = (r.get("workload") or "").strip() if isinstance(r, dict) else ""

                if DEBUG_DUMP_RAW and keep_rec(ns, wl):
                    print(f"  [LN {ln:>3}] raw: {raw_line}")
                if r.get("_invalid"):
                    invalid_records.append({"source": path, "line": ln, "reason": r.get("_reason","parse_error")})
                    reason_counts[r.get("_reason","parse_error")] += 1
                    if DEBUG_DUMP_RAW and keep_rec(ns, wl):
                        print(f"            parsed: <INVALID: {r.get('_reason')}>")
                    continue

                # parsed view
                end_in = r.get("end_input") or r.get("end_date") or ""
                end_dt = parse_date(end_in) or parse_date(r.get("end_date") or "")
                m247 = as_bool(r.get("on_exeption_247"))
                mow  = as_bool(r.get("on_exeption_out_worktime"))
                requester = (r.get("requester") or "").strip()
                reason    = (r.get("reason") or "").strip()
                patcher   = (r.get("created_by") or "").strip()
                if DEBUG_DUMP_RAW and keep_rec(ns, wl):
                    print(f"            parsed: ns={ns}, wl={wl}, m247={m247}, out={mow}, end_in='{end_in}', end_dt={end_dt}, requester='{requester}', reason='{reason}', patcher='{patcher}'")

                if not ns or not wl:
                    invalid_records.append({"source": path, "line": ln, "reason": "missing_ns_or_workload"})
                    reason_counts["missing_ns_or_workload"] += 1
                    continue
                if not (m247 or mow):
                    invalid_records.append({"source": path, "line": ln, "reason": "no_mode", "ns":ns, "workload":wl})
                    reason_counts["no_mode"] += 1
                    continue

                key = f"{ns}|{wl}"
                if key not in groups:
                    groups[key] = {
                        "ns": ns, "workload": wl,
                        "all_dates": [],             # all candidate end dates
                        "modes": set(),
                        "requesters": set(), "reasons": set(), "patchers": set(),
                        "last_updated_at": None,
                    }
                g = groups[key]
                if end_dt: g["all_dates"].append(end_dt)
                if m247: g["modes"].add("247")
                if mow:  g["modes"].add("out_worktime")
                if requester: g["requesters"].add(requester)
                if reason:    g["reasons"].add(reason)
                if patcher:   g["patchers"].add(patcher)

                ca = (r.get("created_at") or "").strip()
                try:
                    ca_dt = datetime.datetime.fromisoformat(ca.replace("Z","+00:00"))
                    if g["last_updated_at"] is None or ca_dt > g["last_updated_at"]:
                        g["last_updated_at"] = ca_dt
                except Exception:
                    pass

                src_id = f'{os.path.basename(path)}:{r.get("req_id","?")}#{r.get("seq","?")}'
                sources[key].append(src_id)
                parsed_ok += 1

        # --- optional dump groups before filtering
        if DEBUG_DUMP_GROUPS:
            print("\n[DEBUG] GROUPS (pre-filter):")
            for key in sorted(groups.keys(), key=lambda x: x.lower()):
                g = groups[key]
                if not keep_rec(g["ns"], g["workload"]):
                    continue
                dl_list = [{"date": d.isoformat(), "days_left": days_left(d, today)} for d in g["all_dates"]]
                print(f"  - {key}")
                print(f"      all_dates     : {dl_list}")
                print(f"      modes         : {sorted(g['modes'])}")
                print(f"      requesters    : {sorted(g['requesters'])}")
                print(f"      reasons       : {sorted(g['reasons'])}")
                print(f"      patchers      : {sorted(g['patchers'])}")
                print(f"      last_updated  : {g['last_updated_at']}")

        # write outputs
        polished_jsonl = os.path.join(OUT_DIR, "polished_exceptions.jsonl")
        polished_csv   = os.path.join(OUT_DIR, "polished_exceptions.csv")
        invalid_jsonl  = os.path.join(OUT_DIR, "invalid.jsonl")

        digest_csv   = os.path.join(OUT_DIR, "digest_exceptions.csv")
        digest_md    = os.path.join(OUT_DIR, "digest_exceptions.webex.md")
        digest_html  = os.path.join(OUT_DIR, "digest_exceptions.html")

        valid_count = 0
        digest_rows = []

        with open(polished_jsonl, "w", encoding="utf-8") as fj, \
             open(polished_csv, "w", newline="", encoding="utf-8") as fc, \
             open(invalid_jsonl, "w", encoding="utf-8") as fi:

            cw = csv.writer(fc)
            cw.writerow([
                "ns","workload","mode_effective","modes","end_date","days_left",
                "requesters","reasons","patchers","sources_count","last_updated_at"
            ])

            # flush invalids from parsing stage
            for inv in invalid_records:
                fi.write(json.dumps(inv, ensure_ascii=False) + "\n")

            for key in sorted(groups.keys(), key=lambda x: x.lower()):
                g = groups[key]
                ns = g["ns"]; wl = g["workload"]
                modes = sorted(g["modes"])

                if not keep_rec(ns, wl):
                    continue

                if not modes:
                    rec = {"ns":ns,"workload":wl,"reason":"no_mode"}
                    fi.write(json.dumps(rec)+"\n")
                    continue

                # chọn end_date: ưu tiên max trong [0..MAX_DAYS]
                in_window = [d for d in g["all_dates"] if d and 0 <= days_left(d, today) <= MAX_DAYS]
                end_d = max(in_window) if in_window else (max(g["all_dates"]) if g["all_dates"] else None)
                if not end_d:
                    rec = {"ns":ns,"workload":wl,"reason":"missing_end_date"}
                    fi.write(json.dumps(rec)+"\n")
                    continue

                mode_eff = "247" if "247" in modes else "out_worktime"
                dl = days_left(end_d, today)
                record = {
                    "ns": ns, "workload": wl, "mode_effective": mode_eff, "modes": modes,
                    "end_date": end_d.isoformat(), "days_left": dl,
                    "requesters": sorted(g["requesters"]), "reasons": sorted(g["reasons"]),
                    "patchers": sorted(g["patchers"]), "sources": sources[key],
                    "sources_count": len(sources[key]),
                    "last_updated_at": g["last_updated_at"].isoformat() if g["last_updated_at"] else None,
                }

                if not (0 <= dl <= MAX_DAYS):
                    inv = {**record, "reason":"all_outside_window"}
                    try:
                        inv["latest_end"] = max(g["all_dates"]).isoformat() if g["all_dates"] else None
                    except Exception:
                        pass
                    fi.write(json.dumps(inv, ensure_ascii=False) + "\n")
                    continue

                # machine-friendly
                fj.write(json.dumps(record, ensure_ascii=False) + "\n")
                cw.writerow([
                    ns, wl, mode_eff, ";".join(modes), end_d.isoformat(), dl,
                    ";".join(record["requesters"]), ";".join(record["reasons"]),
                    ";".join(record["patchers"]), record["sources_count"], record["last_updated_at"] or ""
                ])
                valid_count += 1

                # digest row for humans
                digest_rows.append({
                    "ns": ns,
                    "workload": wl,
                    "mode": mode_human(mode_eff),
                    "end": end_d.isoformat(),
                    "days_left": dl,
                    "reasons": ";".join(record["reasons"]),
                    "requesters": ";".join(record["requesters"]),
                    "patchers": ";".join(record["patchers"]),
                    "tag": "⚠️" if dl <= 3 else ""
                })

        # ---- DIGEST OUTPUTS (human-friendly) ----
        digest_rows.sort(key=lambda r: (r["days_left"], r["ns"].lower(), r["workload"].lower()))

        # CSV
        with open(digest_csv, "w", newline="", encoding="utf-8") as fdc:
            w = csv.writer(fdc)
            w.writerow(["NS","Workload","Mode","End","D-left","Tag","Reason(s)","Requester(s)","Patcher(s)"])
            for r in digest_rows:
                w.writerow([
                    r["ns"], r["workload"], r["mode"], r["end"],
                    r["days_left"], r["tag"], r["reasons"], r["requesters"], r["patchers"]
                ])

        # Webex Markdown
        with open(digest_md, "w", encoding="utf-8") as fdm:
            fdm.write("| NS | Workload | Mode | End | D-left | Tag | Reason(s) | Requester(s) | Patcher(s) |\n")
            fdm.write("| --- | --- | --- | --- | ---: | :-: | --- | --- | --- |\n")
            for r in digest_rows:
                fdm.write(
                    f"| {r['ns']} | {r['workload']} | {r['mode']} | {r['end']} | {r['days_left']} | {r['tag']} | "
                    f"{r['reasons']} | {r['requesters']} | {r['patchers']} |\n"
                )

        # HTML
        with open(digest_html, "w", encoding="utf-8") as fdh:
            fdh.write("<!doctype html><meta charset='utf-8'>\n")
            fdh.write("<style>table{border-collapse:collapse;font:14px sans-serif} th,td{border:1px solid #ddd;padding:6px 8px} th{background:#f6f6f6} .hot{background:#fff3cd}</style>\n")
            fdh.write("<table><thead><tr>"
                      "<th>NS</th><th>Workload</th><th>Mode</th><th>End</th>"
                      "<th style='text-align:right'>D-left</th><th>Tag</th><th>Reason(s)</th><th>Requester(s)</th><th>Patcher(s)</th>"
                      "</tr></thead><tbody>\n")
            for r in digest_rows:
                cls = " class='hot'" if r["tag"] == "⚠️" else ""
                fdh.write(
                    f"<tr{cls}><td>{esc_html(r['ns'])}</td>"
                    f"<td>{esc_html(r['workload'])}</td>"
                    f"<td>{esc_html(r['mode'])}</td>"
                    f"<td>{esc_html(r['end'])}</td>"
                    f"<td style='text-align:right'>{r['days_left']}</td>"
                    f"<td style='text-align:center'>{esc_html(r['tag'])}</td>"
                    f"<td>{esc_html(r['reasons'])}</td>"
                    f"<td>{esc_html(r['requesters'])}</td>"
                    f"<td>{esc_html(r['patchers'])}</td></tr>\n"
                )
            fdh.write("</tbody></table>\n")

        # summary + invalid reason breakdown
        total_invalid = 0
        if os.path.exists(invalid_jsonl):
            with open(invalid_jsonl, "r", encoding="utf-8") as fi:
                for _ in fi:
                    total_invalid += 1

        print(f"📊 Summary: today={today.isoformat()}, raw_files={len(raw_files)}, raw_lines={total_lines}, parsed_ok={parsed_ok}, groups={len(groups)}, polished={valid_count}, invalid_lines={total_invalid}")
        if os.path.exists(invalid_jsonl) and DEBUG:
            reason_counts = defaultdict(int)
            with open(invalid_jsonl, "r", encoding="utf-8") as fi:
                for line in fi:
                    try:
                        obj = json.loads(line)
                        reason_counts[obj.get("reason","(none)")] += 1
                    except Exception:
                        pass
            if reason_counts:
                print("   Invalid breakdown:", dict(sorted(reason_counts.items())))

        print(f"✅ Polished: {polished_jsonl}")
        print(f"✅ Polished: {polished_csv}")
        print(f"ℹ️  Invalid: {invalid_jsonl}")
        print(f"📤 Digest:  {digest_csv}")
        print(f"📤 Webex:   {digest_md}")
        print(f"📤 Email:   {digest_html}")

    finally:
        try:
            os.rmdir(lock_dir)
        except Exception:
            pass

if __name__ == "__main__":
    main()
