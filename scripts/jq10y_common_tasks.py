#!/usr/bin/env python3
"""10Y-COMMON §8 共通タスク D-4〜D-8 を実装する（D-0〜D-3・D-9 は別スクリプト）。

前提: `jq10y_build_db.py`（bars/fins_summary/earnings_date/master 全テーブル）・
`jq10y_compute_calendar.py`・`jq10y_build_universe.py` が完了していること。

出力: `research/EXP-OBS000007/10-result/d4_d8_common_tasks.json`
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

import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jq10y_common import DB_PATH, RAW_DIR, load_calendar, load_universe, parse_ul_ll_flag, AnomalousFlagValueError  # noqa: E402

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000007" / "10-result"


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
    v1_pass = all((v is None or v >= 0.5) for v in per_year_ratio_of_latest.values()) if per_year_ratio_of_latest else False

    # --- 10Y-COMMON §8.1.2（第2版。D-4 判定式の暦日按分ベース改訂） ---
    v2 = d4_v2_calendar_prorated(dict(years_sorted))

    return {
        "total_records": len(rows),
        "records_by_year": dict(years_sorted),
        "ratio_of_latest_year_by_year": per_year_ratio_of_latest,
        "doctype_distribution": dict(doctype_counter.most_common()),
        "field_nonnull_rate": field_nonnull_rate,
        "pass_condition_v1_first_edition": "全期間で年あたりレコード件数が直近年の50%以上（第1版。比較基盤に構造的欠陥あり。廃止済みだが比較可能性のため保持）",
        "pass_v1_first_edition": v1_pass,
        "d4_v2_days_in_window_by_year": v2["days_in_window_by_year"],
        "d4_v2_coverage_ratio_by_year": v2["coverage_ratio_by_year"],
        "d4_v2_is_full_year_by_year": v2["is_full_year_by_year"],
        "d4_v2_density_by_year": v2["density_by_year"],
        "d4_v2_reference_density": v2["reference_density"],
        "d4_v2_expected_by_year": v2["expected_by_year"],
        "d4_v2_pass_by_year": v2["pass_by_year"],
        "d4_v2_pass": v2["pass"],
        "d4_v2_methodology": (
            "10Y-COMMON §8.1.2の暦日按分判定式。coverage_ratio(y)=days_in_window(y)/days_full(y)、"
            "is_full_year(y)=coverage_ratio(y)>=0.95、reference_density=is_full_year=trueの年のdensity(y)の中央値、"
            "expected(y)=reference_density*days_in_window(y)。合格条件: 全年でrecords_by_year(y)>=0.50*expected(y)。"
            "50%という閾値自体は第1版から変更していない。"
        ),
        # 正本は d4_v2_pass。v1系のキーは比較可能性のために残す（10Y-COMMON §8.1.3-2）。
        "pass": v2["pass"],
    }


def d4_v2_calendar_prorated(records_by_year: dict[str, int]) -> dict:
    """10Y-COMMON §8.1.2: D-4 判定式の暦日按分ベース改訂（第2版）。

    T[1]/T[|T|]（契約範囲の実日付）のみから機械的に計算する。実測件数がいくつであっても
    同じ手続きが適用される（§8.1.1 の HARKing非該当根拠4点目）。
    """
    cal = load_calendar()
    t1 = dt.date.fromisoformat(cal["T1"])
    t_last = dt.date.fromisoformat(cal["T_len_date"])

    years = sorted(set(int(y) for y in records_by_year.keys()) | {t1.year, t_last.year})
    days_in_window_by_year: dict[str, int] = {}
    coverage_ratio_by_year: dict[str, float] = {}
    is_full_year_by_year: dict[str, bool] = {}
    density_by_year: dict[str, float | None] = {}

    for y in years:
        days_full = (dt.date(y, 12, 31) - dt.date(y, 1, 1)).days + 1
        y_start = dt.date(y, 1, 1)
        y_end = dt.date(y, 12, 31)
        lo = max(y_start, t1)
        hi = min(y_end, t_last)
        days_in_window = (hi - lo).days + 1 if hi >= lo else 0
        coverage_ratio = days_in_window / days_full
        is_full = coverage_ratio >= 0.95
        rec_count = records_by_year.get(str(y), 0)
        density = (rec_count / days_in_window) if days_in_window > 0 else None
        ys = str(y)
        days_in_window_by_year[ys] = days_in_window
        coverage_ratio_by_year[ys] = coverage_ratio
        is_full_year_by_year[ys] = is_full
        density_by_year[ys] = density

    full_year_densities = [density_by_year[y] for y in density_by_year if is_full_year_by_year[y] and density_by_year[y] is not None]
    if full_year_densities:
        s = sorted(full_year_densities)
        n = len(s)
        mid = n // 2
        reference_density = s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0
    else:
        reference_density = None

    expected_by_year: dict[str, float | None] = {}
    pass_by_year: dict[str, bool] = {}
    for y in years:
        ys = str(y)
        if reference_density is None:
            expected_by_year[ys] = None
            pass_by_year[ys] = False
            continue
        expected = reference_density * days_in_window_by_year[ys]
        expected_by_year[ys] = expected
        rec_count = records_by_year.get(ys, 0)
        pass_by_year[ys] = (expected == 0) or (rec_count >= 0.50 * expected)

    overall_pass = (reference_density is not None) and all(pass_by_year.values())

    return {
        "days_in_window_by_year": days_in_window_by_year,
        "coverage_ratio_by_year": coverage_ratio_by_year,
        "is_full_year_by_year": is_full_year_by_year,
        "density_by_year": density_by_year,
        "reference_density": reference_density,
        "expected_by_year": expected_by_year,
        "pass_by_year": pass_by_year,
        "pass": overall_pass,
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

    # --- 10Y-COMMON §8.2.3: AdjFactor≠1.0 の全2,592行について、C(t)/AdjC(t) が
    #     調整日以外の区間で区分的に一定（累積調整規約）であることを全件検算する。
    full_check = d6_cumulative_convention_check_all_rows(conn)

    return {
        "adjfactor_non1_row_count": total_non1,
        "ext_unique_values_among_adjfactor_non1": dict(ext_values),
        "ext_eq_1_matches_adjfactor_non1_count": ext_eq_1_and_non1_factor,
        "consistent_ext1_iff_split": consistent,
        "unresolved_exrt_row_count": total_non1 - ext_eq_1_and_non1_factor,
        "sample_verification_top5_codes": verification,
        "d6_cumulative_convention_check_all_rows": full_check,
        "methodology": (
            "AdjFactor!=1.0の全行についてExRTのユニーク値を集計し、'1'（権利落ち）との対応を確認した。"
            "10Y-COMMON §8.2.3の指示どおり、AdjFactor!=1.0の全行（ExRT='1'/'2'/'3'いずれも含む）について、"
            "C(t)/AdjC(t)比率が調整日を境界とする区間内で一定（浮動小数点誤差1e-3以内）であることを全件検算した"
            "（d6_cumulative_convention_check_all_rows）。これが全件合格すれば、ExRTの意味（'2'=754件/'3'=23件）が"
            "未解決のままでも、PEADが使う調整系列AdjO/AdjCの累積規約の正しさは独立に確認できたことになる"
            "（10Y-COMMON §8.2.4）。"
        ),
    }


def d6_cumulative_convention_check_all_rows(conn: sqlite3.Connection) -> dict:
    """10Y-COMMON §8.2.3: AdjFactor!=1.0 の全行（ExRT問わず）について、
    C(t)/AdjC(t) が調整日を境界とする区間内で区分的に一定であることを検算する。

    手続き: 銘柄ごとに全期間の (date, c, adjc, adjfactor) を日付昇順で読み、
    AdjFactor!=1.0 の行（調整イベント日）が現れた時点でその行の比率を新しい区間の
    基準値とし、以降の非調整日の比率がその基準値と一致する（相対誤差1e-3以内）ことを確認する。
    """
    codes_with_adj = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM bars WHERE adjfactor IS NOT NULL AND adjfactor != 1.0"
        ).fetchall()
    ]
    total_checked = 0
    total_mismatch = 0
    mismatch_examples: list[dict] = []
    boundary_rows_total = 0
    for code in codes_with_adj:
        series = conn.execute(
            "SELECT date, c, adjc, adjfactor FROM bars WHERE code=? ORDER BY date", (code,)
        ).fetchall()
        segment_ref: float | None = None
        for date, c, adjc, af in series:
            if c is None or adjc is None or adjc == 0:
                continue
            ratio = c / adjc
            is_boundary = af is not None and af != 1.0
            if is_boundary:
                boundary_rows_total += 1
                segment_ref = ratio
                continue
            if segment_ref is None:
                segment_ref = ratio
                continue
            total_checked += 1
            tol = 1e-3 * max(1.0, abs(segment_ref))
            if abs(ratio - segment_ref) > tol:
                total_mismatch += 1
                if len(mismatch_examples) < 20:
                    mismatch_examples.append(
                        {"code": code, "date": date, "ratio": ratio, "segment_ref": segment_ref, "adjfactor": af}
                    )
    return {
        "codes_with_adjfactor_ne1_count": len(codes_with_adj),
        "boundary_rows_total": boundary_rows_total,
        "non_boundary_rows_checked": total_checked,
        "mismatch_count": total_mismatch,
        "mismatch_examples": mismatch_examples,
        "tolerance": "relative 1e-3",
        "pass": total_mismatch == 0,
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
    log(
        f"D4(v2 calendar-prorated) pass={d4['pass']}  "
        f"D6(§8.2.3 PEAD-side cumulative convention, all rows) pass={d6['d6_cumulative_convention_check_all_rows']['pass']}  "
        f"D7 pass={d7['pass']}  D8 pass={d8['pass']} D8 rate={d8['overall_missing_rate']}"
    )
    if not d4["pass"]:
        log("STOP: D-4（暦日按分ベース判定式）が不成立。実験を回さずSに差し戻す。")
        return 4
    if not d7["pass"]:
        log("STOP: D-7（UL/LL 値域・欠損率）が不成立。実験を回さずSに差し戻す。")
        return 7
    if not d8["pass"]:
        log("STOP: D-8（欠損営業日率）が不成立。実験を回さずSに差し戻す。")
        return 8
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
