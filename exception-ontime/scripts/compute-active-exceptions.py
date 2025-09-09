#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compute ACTIVE exceptions from polished_exceptions.jsonl for the current date window.

ENV:
  OUT_DIR       = /data/exceptions/out
  MAX_DAYS      = 60
  TODAY         = YYYY-MM-DD (optional override, e.g. 2025-09-09)
  DEBUG         = 0/1
"""

import os, sys, json, csv, datetime, re
from collections import defaultdict

OUT_DIR        = os.environ.get("OUT_DIR", "/data/exceptions/out")
MAX_DAYS       = int(os.environ.get("MAX_DAYS", "60"))
TODAY_OVERRIDE = os.environ.get("TODAY", "").strip()
DEBUG          = os.environ.get("DEBUG","0").lower() in ("1","true","yes")

POLISHED = os.path.join(OUT_DIR, "polished_exceptions.jsonl")
ACTIVE_JL = os.path.join(OUT_DIR, "active_exceptions.jsonl")
ACTIVE_MD = os.path.join(OUT_DIR, "active_exceptions.md")

ALL_KEYS = {"ALL", "_ALL_", "__ALL__", "*"}

def parse_date(s: str):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None

def today():
    if TODAY_OVERRIDE:
        try:
            return datetime.date.fromisoformat(TODAY_OVERRIDE)
        except Exception:
            pass
    return datetime.date.today()

def days_left(d: datetime.date, t: datetime.date) -> int:
    return (d - t).days

def load_polished(path: str):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows

def normalize_all_key(wl: str) -> str:
    return "_ALL_" if wl in ALL_KEYS else wl

def pick_mode_for_specific(ns: str, wl: str, specific_rec: dict, all_rec: dict):
    """
    Trả về record (dict) đã chọn cho workload cụ thể `wl` của namespace `ns`,
    theo rule so end_date giữa cụ thể và ALL.
    Ghi nhớ: record đã là polished, có 'mode_effective' và 'end_date'.
    """
    if not all_rec:
        return specific_rec
    if not specific_rec:
        return all_rec

    ds = parse_date(specific_rec.get("end_date"))
    dg = parse_date(all_rec.get("end_date"))

    if ds and dg:
        # nếu end_date_specific > end_date_all -> chọn cụ thể, else chọn ALL
        return specific_rec if ds > dg else all_rec
    elif ds and not dg:
        return specific_rec
    elif dg and not ds:
        return all_rec
    else:
        # thiếu cả hai -> nghiêng ALL
        return all_rec

def main():
    t = today()
    data = load_polished(POLISHED)

    if DEBUG:
        print(f"[DEBUG] TODAY={t.isoformat()}, MAX_DAYS={MAX_DAYS}")
        print(f"[DEBUG] polished_exceptions.jsonl present={os.path.exists(POLISHED)} rows={len(data)}")

    # index polished by ns|wl + keep ALL per ns
    by_ns = defaultdict(dict)   # ns -> wl -> rec
    by_ns_all = {}              # ns -> rec for ALL (if any)

    for r in data:
        ns = (r.get("ns") or "").strip()
        wl = (r.get("workload") or "").strip()
        if not ns or not wl:
            continue

        end_d = parse_date(r.get("end_date"))
        if not end_d:
            continue

        dl = days_left(end_d, t)
        if not (0 <= dl <= MAX_DAYS):
            # polished đã lọc một lần, nhưng vẫn double-check theo TODAY_OVERRIDE
            continue

        wl_norm = normalize_all_key(wl)
        rec = {
            "ns": ns,
            "workload": wl_norm,
            "mode": r.get("mode_effective",""),
            "end_date": end_d.isoformat(),
            "days_left": dl,
            "sources_count": r.get("sources_count", 1),
            "last_updated_at": r.get("last_updated_at"),
            "modes_raw": r.get("modes"),
            "requesters": r.get("requesters", []),
            "reasons": r.get("reasons", []),
            "patchers": r.get("patchers", []),
        }
        if wl_norm == "_ALL_":
            # nếu nhiều ALL trong cùng ns (hiếm), chọn cái end_date muộn hơn
            prev = by_ns_all.get(ns)
            if (not prev) or (parse_date(rec["end_date"]) > parse_date(prev["end_date"])):
                by_ns_all[ns] = rec
        else:
            # nếu nhiều bản cụ thể cùng key (không nên có do polished đã dedupe),
            # vẫn chọn end_date muộn hơn phòng dữ liệu bất thường.
            prev = by_ns[ns].get(wl_norm)
            if (not prev) or (parse_date(rec["end_date"]) > parse_date(prev["end_date"])):
                by_ns[ns][wl_norm] = rec

    # build ACTIVE set
    active = []

    # 1) emit ALL cho mỗi ns nếu còn hiệu lực (để scaler có thể tham chiếu)
    for ns, all_rec in sorted(by_ns_all.items()):
        active.append({
            **all_rec,
            "ns": ns,
            "workload": "_ALL_",
            "mode": all_rec["mode"] if all_rec.get("mode") in ("247","out_worktime") else "none"
        })

    # 2) emit cụ thể theo rule so end_date với ALL
    for ns, mp in sorted(by_ns.items()):
        all_rec = by_ns_all.get(ns)
        for wl, spec_rec in sorted(mp.items()):
            chosen = pick_mode_for_specific(ns, wl, spec_rec, all_rec)
            mode = chosen.get("mode","none")
            if mode not in ("247","out_worktime"):
                # safety
                continue
            out = {**chosen, "ns": ns, "workload": wl, "mode": mode}
            active.append(out)

    # write outputs
    # jsonl
    with open(ACTIVE_JL, "w", encoding="utf-8") as f:
        for r in active:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # md (để Jenkins cat)
    active_sorted = sorted(active, key=lambda r: (r["ns"].lower(), r["workload"].lower()))
    with open(ACTIVE_MD, "w", encoding="utf-8") as f:
        f.write(f"**Active exceptions @ {t.isoformat()} (MAX_DAYS={MAX_DAYS})**\n\n")
        f.write("| NS | Workload | Mode | End | D-left | Reason(s) | Requester(s) | Patcher(s) |\n")
        f.write("| --- | --- | :-: | --- | ---: | --- | --- | --- |\n")
        for r in active_sorted:
            f.write(
                f"| {r['ns']} | {r['workload']} | {r['mode']} | {r['end_date']} | {r['days_left']} | "
                f"{';'.join(r.get('reasons',[]))} | { ';'.join(r.get('requesters',[])) } | { ';'.join(r.get('patchers',[])) } |\n"
            )

    print(f"✅ Active written: {ACTIVE_JL}")
    print(f"📝 Active digest: {ACTIVE_MD}")
    print(f"📦 Count: {len(active)}")

if __name__ == "__main__":
    main()
