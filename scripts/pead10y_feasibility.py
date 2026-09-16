#!/usr/bin/env python3
"""EXP-OBS000005（PEAD・10年版）§9 先行タスクと §6.0 データ十分性ゲート（DS-1〜DS-7）。

前提: `jq10y_build_db.py`・`jq10y_compute_calendar.py`・`jq10y_build_universe.py`・
`jq10y_common_tasks.py` が完了していること。

出力:
  - `research/EXP-OBS000005/10-result/feasibility.json`
  - `research/EXP-OBS000005/10-result/params.json`（既存D-0〜D-8の内容に9-1〜9-7をマージ）

**リターンを一切参照しない。** カウント・銘柄数・日数・欠損率のみで評価する。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jq10y_common import (  # noqa: E402
    DB_PATH, RAW_DIR, EVENT_DOCTYPE_RE, EVENT_CURPERTYPE_OK,
    Calendar, UniverseIndex, build_all_disclosures_index, compute_raw_sue_for_code,
    load_calendar, load_universe, median, pctile,
)

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000005" / "10-result"
U6_CAP_LABEL = "pead"

# 検出力の式に入るσ_Rは事前固定の定数（実測値を使わない。10Y-COMMON §7.1原則5）
SIGMA_R_FIXED = 0.04


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def task_9_1(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT disc_date, json_blob FROM fins_summary").fetchall()
    doctype_by_year: dict[str, Counter] = defaultdict(Counter)
    curper_by_year: dict[str, Counter] = defaultdict(Counter)
    doctype_all = Counter()
    curper_all = Counter()
    for disc_date, blob in rows:
        rec = json.loads(blob)
        y = (disc_date or "")[:4]
        dtv, cpv = rec.get("DocType"), rec.get("CurPerType")
        doctype_by_year[y][dtv] += 1
        curper_by_year[y][cpv] += 1
        doctype_all[dtv] += 1
        curper_all[cpv] += 1

    event_doctype_values = {k for k in doctype_all if EVENT_DOCTYPE_RE.match(k or "")}
    non_event_doctype_values = {k for k in doctype_all if not EVENT_DOCTYPE_RE.match(k or "")}
    unmapped_curper = {k for k in curper_all if k not in EVENT_CURPERTYPE_OK} - {""}

    return {
        "doctype_unique_values_and_counts_all": dict(doctype_all.most_common()),
        "curpertype_unique_values_and_counts_all": dict(curper_all.most_common()),
        "doctype_by_year": {y: dict(c.most_common()) for y, c in sorted(doctype_by_year.items())},
        "curpertype_by_year": {y: dict(c.most_common()) for y, c in sorted(curper_by_year.items())},
        "mapping_used": {
            "event_doctype_pattern": EVENT_DOCTYPE_RE.pattern,
            "event_doctype_matched_values": sorted(event_doctype_values),
            "non_event_doctype_values": sorted(non_event_doctype_values),
            "event_curpertype_values": sorted(EVENT_CURPERTYPE_OK),
            "curpertype_values_not_in_event_set": sorted(unmapped_curper),
        },
        "mapping_ambiguous": False,
    }


def compute_all_sue_events(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT json_blob FROM fins_summary").fetchall()
    records = [json.loads(r[0]) for r in rows]
    by_code = build_all_disclosures_index(records)
    all_events = []
    for code, disclosures in by_code.items():
        all_events.extend(compute_raw_sue_for_code(disclosures))
    return all_events


def task_9_2(all_events: list[dict]) -> dict:
    by_year: dict[str, list[float]] = defaultdict(list)
    excluded_by_year: dict[str, Counter] = defaultdict(Counter)
    for e in all_events:
        y = (e["disc_date"] or "")[:4]
        if e["raw_sue"] is not None:
            by_year[y].append(e["raw_sue"])
        elif e["excluded_reason"]:
            excluded_by_year[y][e["excluded_reason"]] += 1

    def stats_for(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0}
        s = sorted(vals)
        n = len(s)
        exact_zero = sum(1 for v in vals if v == 0.0)
        return {
            "n": n, "min": s[0], "p01": pctile(s, 0.01), "p10": pctile(s, 0.10),
            "median": pctile(s, 0.50), "p90": pctile(s, 0.90), "p99": pctile(s, 0.99), "max": s[-1],
            "mean": sum(vals) / n, "exact_zero_count": exact_zero, "exact_zero_ratio": exact_zero / n,
        }

    return {
        "by_year": {y: stats_for(v) for y, v in sorted(by_year.items())},
        "excluded_by_year": {y: dict(c) for y, c in sorted(excluded_by_year.items())},
        "overall": stats_for([v for vs in by_year.values() for v in vs]),
    }


def in_universe_events(all_events: list[dict], uidx: UniverseIndex) -> list[dict]:
    out = []
    for e in all_events:
        if e["raw_sue"] is None:
            continue
        codes = uidx.codes_for(e["disc_date"])
        if e["code"] in codes:
            out.append(e)
    return out


def task_ds_gates(all_events: list[dict], cal: Calendar, uidx: UniverseIndex, universe_json: dict, d4d8: dict) -> dict:
    sel_lo, sel_hi = cal.selection_range
    conf_lo, conf_hi = cal.confirmation_range

    valid_in_universe = in_universe_events(all_events, uidx)
    conf_events = [e for e in valid_in_universe if conf_lo <= e["disc_date"] <= conf_hi]
    sel_events = [e for e in valid_in_universe if sel_lo <= e["disc_date"] <= sel_hi]

    ds1_count = len(conf_events)
    ds1_pass = ds1_count >= 1200

    by_date = Counter(e["disc_date"] for e in conf_events)
    days_ge5 = {d: c for d, c in by_date.items() if c >= 5}
    ds2a_days = len(days_ge5)
    ds2a_pass = ds2a_days >= 120
    n_d_avg = (sum(days_ge5.values()) / len(days_ge5)) if days_ge5 else None
    mde_pre = None
    if n_d_avg and n_d_avg > 1 and ds2a_days > 0:
        mde_pre = 1.645 / (((n_d_avg - 1) ** 0.5) * (ds2a_days ** 0.5))
    ds2b_pass = (mde_pre is not None) and (mde_pre <= 0.055)

    ds3_results = {}
    for rec in universe_json["per_date"]:
        ds3_results[rec["date"]] = rec["by_u6_cap"][U6_CAP_LABEL]["final_count"]
    ds3_pass = all(v >= 150 for v in ds3_results.values())

    ds4_missing_rate = d4d8["D8_missing_business_day_rate"]["overall_missing_rate"]
    ds4_pass = (ds4_missing_rate is not None) and (ds4_missing_rate <= 0.05)

    max_day_count = max(by_date.values()) if by_date else 0
    ds5_share = (max_day_count / ds1_count) if ds1_count else None
    ds5_pass = (ds5_share is not None) and (ds5_share <= 0.10)

    ds6_days = len(by_date)
    ds6_pass = ds6_days >= 400

    def quarter_key(d: str) -> str:
        y, m = int(d[:4]), int(d[5:7])
        q = (m - 1) // 3 + 1
        return f"{y}Q{q}"

    quarters = {quarter_key(d) for d in by_date}
    ds7_count = len(quarters)
    ds7_pass = ds7_count >= 20

    all_pass = ds1_pass and ds2a_pass and ds2b_pass and ds3_pass and ds4_pass and ds5_pass and ds6_pass and ds7_pass

    # 経済的必要IC水準 (G1-3から逆算。C品質チームが独立再計算できるよう定数を明記)
    ic_econ_at_fixed_sigma = 0.0035 / (1.755 * SIGMA_R_FIXED)

    return {
        "selection_range": [sel_lo, sel_hi],
        "confirmation_range": [conf_lo, conf_hi],
        "selection_event_count": len(sel_events),
        "DS-1_confirmation_valid_event_count": ds1_count,
        "DS-1_threshold": 1200,
        "DS-1_pass": ds1_pass,
        "DS-2a_days_with_ge5_events_K": ds2a_days,
        "DS-2a_threshold": 120,
        "DS-2a_pass": ds2a_pass,
        "DS-2a_avg_events_per_K_day_n_d": n_d_avg,
        "DS-2b_MDE_pre": mde_pre,
        "DS-2b_threshold": 0.055,
        "DS-2b_pass": ds2b_pass,
        "DS-2b_formula": "1.645 / (sqrt(n_d-1) * sqrt(K))",
        "DS-3_universe_count_by_date": ds3_results,
        "DS-3_threshold": 150,
        "DS-3_pass": ds3_pass,
        "DS-4_missing_business_day_rate": ds4_missing_rate,
        "DS-4_threshold": 0.05,
        "DS-4_pass": ds4_pass,
        "DS-5_max_single_day_share": ds5_share,
        "DS-5_max_single_day_count": max_day_count,
        "DS-5_threshold": 0.10,
        "DS-5_pass": ds5_pass,
        "DS-6_distinct_disclosure_days": ds6_days,
        "DS-6_threshold": 400,
        "DS-6_pass": ds6_pass,
        "DS-7_distinct_calendar_quarters": ds7_count,
        "DS-7_threshold": 20,
        "DS-7_pass": ds7_pass,
        "all_pass": all_pass,
        "sigma_R_fixed_const": SIGMA_R_FIXED,
        "IC_econ_at_fixed_sigma_R": ic_econ_at_fixed_sigma,
        "events_per_day_distribution": {
            "min": min(by_date.values()) if by_date else None,
            "median": median(list(by_date.values())) if by_date else None,
            "max": max_day_count if by_date else None,
        },
        "top10_event_days": sorted(by_date.items(), key=lambda kv: -kv[1])[:10],
        "quarterly_event_counts": {q: sum(1 for d in by_date if quarter_key(d) == q) for q in sorted(quarters)},
    }


def task_9_7_exclusion_breakdown(all_events: list[dict]) -> dict:
    excl = [e for e in all_events if e["excluded_reason"]]
    by_field_reason = Counter(e["excluded_reason"] for e in excl)
    by_year = defaultdict(Counter)
    for e in excl:
        by_year[(e["disc_date"] or "")[:4]][e["excluded_reason"]] += 1
    return {
        "total_excluded": len(excl),
        "by_reason": dict(by_field_reason),
        "by_year_and_reason": {y: dict(c) for y, c in sorted(by_year.items())},
    }


def task_9_5_pead_disc_dates(all_events: list[dict]) -> dict:
    valid = [e for e in all_events if e["raw_sue"] is not None]
    disc_dates = sorted({e["disc_date"] for e in valid})
    out_path = RESULT_DIR / "pead_disc_dates.json"
    out_path.write_text(json.dumps(disc_dates, ensure_ascii=False), encoding="utf-8")
    return {"distinct_disc_dates_count": len(disc_dates), "saved_to": str(out_path.relative_to(RESULT_DIR.parent.parent.parent))}


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA query_only=ON")
    cal_json = load_calendar()
    cal = Calendar(cal_json)
    universe_json = load_universe()
    uidx = UniverseIndex(universe_json, U6_CAP_LABEL)
    d4d8 = json.loads((RESULT_DIR / "d4_d8_common_tasks.json").read_text(encoding="utf-8"))

    log("9-1 実行中...")
    t91 = task_9_1(conn)

    log("SUE計算中（全銘柄）...")
    all_events = compute_all_sue_events(conn)
    log(f"  総イベント数={len(all_events)}")

    log("9-2 実行中...")
    t92 = task_9_2(all_events)

    log("DSゲート評価中...")
    ds = task_ds_gates(all_events, cal, uidx, universe_json, d4d8)

    log("9-5 実行中...")
    t95 = task_9_5_pead_disc_dates(all_events)

    log("9-7 実行中...")
    t97 = task_9_7_exclusion_breakdown(all_events)

    feasibility = {
        "9-1_doctype_curpertype_mapping": t91,
        "9-2_raw_sue_descriptive_stats_by_year": t92,
        "9-3_DS_gates": ds,
        "9-5_pead_disc_dates_summary": t95,
        "9-7_exclusion_breakdown": t97,
        "D4_D8_common_tasks_reference": "research/EXP-OBS000005/10-result/d4_d8_common_tasks.json",
    }
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "feasibility.json").write_text(json.dumps(feasibility, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"saved: {RESULT_DIR / 'feasibility.json'}")

    # params.json: 既存内容があれば読み込みマージ、無ければ新規
    params_path = RESULT_DIR / "params.json"
    params = json.loads(params_path.read_text(encoding="utf-8")) if params_path.exists() else {}
    params["calendar"] = cal_json
    params["pead_ds_gate_result"] = ds
    params["pead_random_seed"] = 20260915
    params_path.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"saved: {params_path}")

    log(json.dumps({k: v for k, v in ds.items() if k.endswith("_pass") or k == "all_pass"}, ensure_ascii=False, indent=2))
    return 0 if ds["all_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
