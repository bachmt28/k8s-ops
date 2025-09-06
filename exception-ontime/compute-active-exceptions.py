#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, json, datetime, re

OUT_DIR        = os.environ.get("OUT_DIR", "/data/exceptions/out")
MAX_DAYS       = int(os.environ.get("MAX_DAYS", "60"))
TODAY_OVERRIDE = os.environ.get("TODAY","").strip()   # YYYY-MM-DD
DEBUG          = os.environ.get("DEBUG","0").lower() in ("1","true","yes")

ALL_ALIASES = { "all-of-workload", "all", "*", "__all__", "ALL-OF-WORKLOAD", "ALL", "all-of-workloads", "ALL-OF-WORKLOADS" }

def today():
    if TODAY_OVERRIDE:
        try: return datetime.date.fromisoformat(TODAY_OVERRIDE)
        except: pass
    return datetime.date.today()

def is_all_token(name: str) -> bool:
    return name.strip() in ALL_ALIASES

def main():
    pol = os.path.join(OUT_DIR, "polished_exceptions.jsonl")
    if not os.path.exists(pol):
        print(f"❌ Missing {pol}", flush=True); sys.exit(1)

    t = today()
    active = {}
    kept = 0
    with open(pol, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line=line.strip()
            if not line: continue
            try:
                r = json.loads(line)
            except Exception:
                if DEBUG: print(f"[skip] invalid json at ln={ln}")
                continue

            ns   = (r.get("ns") or "").strip()
            wl   = (r.get("workload") or "").strip()
            mode = (r.get("mode_effective") or "").strip()   # '247' | 'out_worktime'
            dl   = r.get("days_left", None)

            if not ns or not wl or mode not in ("247","out_worktime"):
                continue
            try:
                dl = int(dl)
            except:
                continue
            if dl < 0 or dl > MAX_DAYS:
                continue

            wl_key = "__ALL__" if is_all_token(wl) else wl
            key = f"{ns}|{wl_key}"
            # last write wins; polished đã theo last_updated
            active[key] = {
                "ns": ns, "workload": wl_key, "mode": mode,
                "end_date": r.get("end_date"), "days_left": dl,
                "patchers": r.get("patchers") or [], "requesters": r.get("requesters") or [],
            }
            kept += 1

    out_jsonl = os.path.join(OUT_DIR, "active_exceptions.jsonl")
    with open(out_jsonl, "w", encoding="utf-8") as fj:
        for rec in active.values():
            fj.write(json.dumps(rec, ensure_ascii=False) + "\n")

    out_md = os.path.join(OUT_DIR, "active_exceptions.md")
    with open(out_md, "w", encoding="utf-8") as fm:
        fm.write(f"**Active exceptions @ {t.isoformat()}**\n\n")
        fm.write("| NS | Workload | Mode | End | D-left |\n")
        fm.write("| --- | --- | --- | --- | ---: |\n")
        for rec in sorted(active.values(), key=lambda x: (x["ns"].lower(), x["workload"].lower())):
            mode_label = "24/7" if rec["mode"]=="247" else "Ngoài giờ"
            fm.write(f"| {rec['ns']} | {rec['workload']} | {mode_label} | {rec.get('end_date','')} | {rec['days_left']} |\n")

    print(f"✅ Active list: {out_jsonl} (kept={kept})")
    print(f"📝 Preview:     {out_md}")

if __name__ == "__main__":
    main()
