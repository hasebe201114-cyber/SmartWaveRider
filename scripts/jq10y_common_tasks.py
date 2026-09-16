#!/usr/bin/env python3
"""10Y-COMMON §8 共通タスク D-4〜D-8 を実装する（D-0〜D-3・D-9 は別スクリプト）。

前提: `jq10y_build_db.py`（bars/fins_summary/earnings_date/master 全テーブル）・
`jq10y_compute_calendar.py`・`jq10y_build_universe.py` が完了していること。

出力: `research/EXP-OBS000005/10-result/d4_d8_common_tasks.json`
  - D-4: /fins/summary 10年カバレッジ
  - D-5: /fins/earnings-date 実カバレッジ範囲
  - D-6: 調整規約（AdjFactor/ExRT）の実測確定
  - D-7: UL/LL の全ユニーク値・件数・欠損率（10年全期間）
  - D-8: 欠損営業日率（PEAD+Gap 確定ユニバースの和集合 × 全期間）

判定語は書かない。数値と事実のみ。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jq10y_common import DB_PATH, RAW_DIR, load_calendar, load_universe, parse_ul_ll_flag, AnomalousFlagValueError  # noqa: E402

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000005" / "10-result"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def d4_fins_summary_coverage(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT disc_date, doctype, json_blob FROM fins_summary").fetchall()
    by_year = Counter()
    doctype_counter = Counter()
    field_nonnull = {"FOdP": 0, "OdP": 0, "NxFOdP": 0, "FSales": 0, "NxFSales": 0}
    field_total = 0
    for disc_date, doctype, blob in rows:
        if disc_date:
            by_year[disc_date[:4]] += 1
        doctype_counter[doctype] += 1
        rec = json.loads(blob)
        field_total += 1
        for f in field_nonnull:
            v = rec.get(f)
            if v is not None and str(v).strip() != "":
                field_nonnull[f] += 1
    field_nonnull_rate = {f: (n / field_total if field_total else None) for f, n in field_nonnull.items()}
    years_sorted = sorted(by_year.items())
    latest_year_count = by_year.get(years_sorted[-1][0], 0) if years_sorted else 0
    per_year_ratio_of_latest = {y: (c / latest_year_count if latest_year_count else None) for y, c in years_sorted}
    return {
        "total_records": len(rows),
        "records_by_year": dict(years_sorted),
        "ratio_of_latest_year_by_year": per_year_ratio_of_latest,
        "doctype_distribution": dict(doctype_counter.most_common()),
        "field_nonnull_rate": field_nonnull_rate,
        "pass_condition": "全期間で年あたりレコード件数が直近年の50%以上",
        "pass": all((v is None or v >= 0.5) for v in per_year_ratio_of_latest.values()) if per_year_ratio_of_latest else False,
    }


def d5_earnings_date_coverage(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT pubdate, schdate FROM earnings_date").fetchall()
    pubdates = [r[0] for r in rows if r[0]]
    schdates = [r[1] for r in rows if r[1]]
    by_year = Counter(p[:4] for p in pubdates)
    return {
        "total_records": len(rows),
        "pubdate_min": min(pubdates) if pubdates else None,
        "pubdate_max": max(pubdates) if pubdates else None,
        "schdate_min": min(schdates) if schdates else None,
        "schdate_max": max(schdates) if schdates else None,
        "records_by_pubdate_year": dict(sorted(by_year.items())),
    }


def d6_adjustment_factor(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT code, date, adjfactor, ext, c, adjc FROM bars WHERE adjfactor IS NOT NULL AND adjfactor != 1.0"
    ).fetchall()
    total_non1 = len(rows)
    ext_values = Counter(r[3] for r in rows)
    ext_eq_1_and_non1_factor = sum(1 for r in rows if str(r[3]) == "1")
    consistent = ext_eq_1_and_non1_factor == total_non1

    # 検算: サンプル銘柄（AdjFactor!=1.0が最も多い上位5銘柄）について、
    # 調整系列 AdjC が分割日をまたいで段差を持たないことを確認する
    # （分割日以降、C/AdjC比が一定＝累積調整であることのチェック）。
    codes_with_split = Counter(r[0] for r in rows)
    sample_codes = [c for c, _ in codes_with_split.most_common(5)]
    verification = []
    for code in sample_codes:
        series = conn.execute(
            "SELECT date, c, adjc, adjfactor FROM bars WHERE code=? ORDER BY date", (code,)
        ).fetchall()
        ratios = []
        for date, c, adjc, af in series:
            if c and adjc and c != 0:
                ratios.append({"date": date, "c_over_adjc": c / adjc, "adjfactor": af})
        # 比率が変化する境界日（分割イベント日）の数
        distinct_ratio_values = sorted({round(r["c_over_adjc"], 4) for r in ratios})
        verification.append({
            "code": code,
            "row_count": len(series),
            "distinct_c_over_adjc_ratio_count": len(distinct_ratio_values),
            "distinct_ratios_sample": distinct_ratio_values[:10],
        })

    return {
        "adjfactor_non1_row_count": total_non1,
        "ext_unique_values_among_adjfactor_non1": dict(ext_values),
        "ext_eq_1_matches_adjfactor_non1_count": ext_eq_1_and_non1_factor,
        "consistent_ext1_iff_split": consistent,
        "sample_verification_top5_codes": verification,
        "methodology": (
            "AdjFactor!=1.0の全行についてExRTのユニーク値を集計し、'1'（権利落ち）との対応を確認した。"
            "サンプル検算では、C/AdjCの比率が分割イベント日境界でのみ変化し、それ以外は区間内で一定"
            "（＝累積調整規約）であることを、比率のユニーク値数で確認する。"
        ),
    }


def d7_ul_ll_validation(conn: sqlite3.Connection) -> dict:
    ul_counter: Counter = Counter()
    ll_counter: Counter = Counter()
    total = 0
    ul_null = 0
    ll_null = 0
    anomalous: set = set()
    cur = conn.execute("SELECT ul, ll FROM bars")
    for ul_raw, ll_raw in cur:
        total += 1
        ul_counter[repr(ul_raw)] += 1
        ll_counter[repr(ll_raw)] += 1
        for raw, is_ul in ((ul_raw, True), (ll_raw, False)):
            try:
                parsed = parse_ul_ll_flag(raw)
                if parsed is None:
                    if is_ul:
                        ul_null += 1
                    else:
                        ll_null += 1
            except AnomalousFlagValueError:
                anomalous.add(repr(raw))
    missing = ul_null + ll_null
    denom = total * 2
    missing_rate = missing / denom if denom else None
    return {
        "total_rows_scanned": total,
        "ul_unique_values_and_counts": dict(ul_counter),
        "ll_unique_values_and_counts": dict(ll_counter),
        "ul_null_count": ul_null,
        "ll_null_count": ll_null,
        "combined_missing_rate": missing_rate,
        "anomalous_values": sorted(anomalous),
        "values_confined_to_1_0_empty_null": len(anomalous) == 0,
        "pass": len(anomalous) == 0 and (missing_rate is not None) and (missing_rate <= 0.01),
    }


def d8_missing_business_day_rate(conn: sqlite3.Connection, cal: dict, universe: dict) -> dict:
    T = cal["T"]
    T_set_sorted = T
    # 対象ユニバース: PEAD(271) と Gap(175) の確定ユニバースの和集合（全確定日を通じて）
    union_codes: set[str] = set()
    for rec in universe["per_date"]:
        for label in ("pead", "gap"):
            union_codes |= set(rec["by_u6_cap"][label]["codes"])

    total_expected = 0
    total_actual = 0
    worst = []
    for code in union_codes:
        rows = conn.execute("SELECT date FROM bars WHERE code=? ORDER BY date", (code,)).fetchall()
        dates = [r[0] for r in rows]
        if not dates:
            continue
        code_min, code_max = dates[0], dates[-1]
        import bisect as _bisect
        lo = _bisect.bisect_left(T_set_sorted, code_min)
        hi = _bisect.bisect_right(T_set_sorted, code_max)
        expected = hi - lo
        actual = len(dates)
        missing = expected - actual
        total_expected += expected
        total_actual += actual
        if expected:
            worst.append({"code": code, "expected": expected, "actual": actual, "missing_rate": missing / expected})
    worst.sort(key=lambda r: -r["missing_rate"])
    overall = (total_expected - total_actual) / total_expected if total_expected else None
    return {
        "universe_codes_count": len(union_codes),
        "total_expected_code_days": total_expected,
        "total_actual_code_days": total_actual,
        "overall_missing_rate": overall,
        "worst_20": worst[:20],
        "methodology": (
            "PEAD(U6_cap=271)とGap(U6_cap=175)の全確定日にわたる確定ユニバースの和集合を対象銘柄とし、"
            "各銘柄について、その銘柄の最初の観測日〜最後の観測日の範囲内にあるTの日数を期待日数、"
            "実際に行が存在する日数を実日数として欠損率を算出した。"
        ),
        "pass": (overall is not None) and (overall <= 0.05),
    }


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA query_only=ON")
    cal = load_calendar()
    universe = load_universe()

    log("D-4 実行中...")
    d4 = d4_fins_summary_coverage(conn)
    log("D-5 実行中...")
    d5 = d5_earnings_date_coverage(conn)
    log("D-6 実行中...")
    d6 = d6_adjustment_factor(conn)
    log("D-7 実行中...")
    d7 = d7_ul_ll_validation(conn)
    log("D-8 実行中...")
    d8 = d8_missing_business_day_rate(conn, cal, universe)

    out = {"D4_fins_summary_coverage": d4, "D5_earnings_date_coverage": d5,
           "D6_adjustment_factor": d6, "D7_ul_ll_validation": d7, "D8_missing_business_day_rate": d8}
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / "d4_d8_common_tasks.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"saved: {out_path}")
    log(f"D4 pass={d4['pass']} D7 pass={d7['pass']} D8 pass={d8['pass']} D8 rate={d8['overall_missing_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
