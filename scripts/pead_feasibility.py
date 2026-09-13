#!/usr/bin/env python3
"""EXP-OBS000001（PEAD）§9 先行タスク（9-1〜9-5）の実測スクリプト。

出力:
  - `research/EXP-OBS000001/10-result/params.json`（9-1の値対応付け＋spec固定パラメータの写し）
  - `research/EXP-OBS000001/10-result/feasibility.json`（9-2〜9-5）

前提: `pead_fetch_data.py --step master/fins/bars` と `pead_build_universe.py` が完了していること。

判定語は書かない。すべて実測値・件数のみ。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.pead_common import (  # noqa: E402
    AnomalousFlagValueError,
    EVENT_CURPERTYPE_OK,
    EVENT_DOCTYPE_RE,
    MRGN_U2_OK,
    SCALECAT_U1_OK,
    build_all_disclosures_index,
    compute_raw_sue_for_code,
    is_event_disclosure,
    load_fins_summary,
    parse_ul_ll_flag,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"
RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"

CONTRACT_START = dt.date(2024, 6, 21)
CONTRACT_END = dt.date(2026, 6, 21)
CONFIRMATION_START = dt.date(2025, 7, 1)
CONFIRMATION_END = dt.date(2026, 6, 21)
SELECTION_START = dt.date(2024, 6, 21)
SELECTION_END = dt.date(2025, 6, 30)


# ---------- 9-1: 値の対応付け ----------


def task_9_1() -> dict:
    sel_master = json.loads(
        (RAW_DIR / "master_selection_start_2024-06-21.json").read_text(encoding="utf-8")
    )["data"]
    conf_master = json.loads(
        (RAW_DIR / "master_confirmation_start_2025-07-01.json").read_text(encoding="utf-8")
    )["data"]

    def tabulate(rows, field):
        c = Counter(r.get(field) for r in rows)
        return {str(k): v for k, v in sorted(c.items(), key=lambda x: -x[1])}

    master_fields = {}
    for field in ["ScaleCat", "Mrgn", "MrgnNm", "S33", "S33Nm"]:
        master_fields[field] = {
            "selection_start_2024-06-21": tabulate(sel_master, field),
            "confirmation_start_2025-07-01": tabulate(conf_master, field),
        }

    fins_records = load_fins_summary()
    doctype_counter = Counter(r.get("DocType") for r in fins_records)
    curper_counter = Counter(r.get("CurPerType") for r in fins_records)

    # DocType/CurPerType の組合せも記録する（イベント分類の妥当性確認用）
    combo_counter = Counter((r.get("DocType"), r.get("CurPerType")) for r in fins_records)

    event_doctype_values = {k for k in doctype_counter if EVENT_DOCTYPE_RE.match(k or "")}
    non_event_doctype_values = {k for k in doctype_counter if not EVENT_DOCTYPE_RE.match(k or "")}
    unmapped_curper = {k for k in curper_counter if k not in EVENT_CURPERTYPE_OK} - {""}

    return {
        "master_field_unique_values": master_fields,
        "fins_summary_total_records_scanned": len(fins_records),
        "doctype_unique_values_and_counts": {
            str(k): v for k, v in sorted(doctype_counter.items(), key=lambda x: -x[1])
        },
        "curpertype_unique_values_and_counts": {
            str(k): v for k, v in sorted(curper_counter.items(), key=lambda x: -x[1])
        },
        "doctype_curpertype_combo_counts": {
            f"{k[0]}|{k[1]}": v for k, v in sorted(combo_counter.items(), key=lambda x: -x[1])
        },
        "mapping_used": {
            "U1_scalecat_topix500_equivalent": sorted(SCALECAT_U1_OK),
            "U2_mrgn_margin_eligible": sorted(MRGN_U2_OK),
            "U2_mrgn_excluded": ["3"],
            "event_doctype_pattern": EVENT_DOCTYPE_RE.pattern,
            "event_doctype_matched_values": sorted(event_doctype_values),
            "non_event_doctype_values": sorted(non_event_doctype_values),
            "event_curpertype_values": sorted(EVENT_CURPERTYPE_OK),
            "curpertype_values_not_in_event_set": sorted(unmapped_curper),
        },
        "mapping_ambiguous": False,
    }


# ---------- 9-1b: UL/LL の全ユニーク値・欠損率（§4.0.3） ----------


def task_9_1b(candidate_codes: list[str]) -> dict:
    ul_counter: Counter = Counter()
    ll_counter: Counter = Counter()
    total_rows = 0
    ul_null = 0
    ll_null = 0
    anomalous_values: set = set()

    for code in candidate_codes:
        bars = load_bars(code)
        if bars is None:
            continue
        for r in bars:
            total_rows += 1
            ul_raw = r.get("UL")
            ll_raw = r.get("LL")
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
                    anomalous_values.add(repr(raw))

    missing_count = ul_null + ll_null
    denom = total_rows * 2
    missing_rate = (missing_count / denom) if denom else None

    values_confined = len(anomalous_values) == 0

    return {
        "total_bars_rows_scanned": total_rows,
        "ul_unique_values_and_counts": dict(ul_counter),
        "ll_unique_values_and_counts": dict(ll_counter),
        "ul_null_count": ul_null,
        "ll_null_count": ll_null,
        "combined_missing_rate": missing_rate,
        "anomalous_values_outside_1_0_empty_null": sorted(anomalous_values),
        "values_confined_to_1_0_empty_null": values_confined,
        "missing_rate_threshold": 0.01,
        "missing_rate_pass": (missing_rate is not None) and (missing_rate <= 0.01),
        "pass": values_confined and (missing_rate is not None) and (missing_rate <= 0.01),
    }


# ---------- 9-2: 日足の欠損営業日率 ----------


def load_bars(code: str) -> list[dict] | None:
    path = RAW_DIR / "bars_daily" / f"{code}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def task_9_2(candidate_codes: list[str]) -> dict:
    bars_state_path = RAW_DIR / "bars_fetch_state.json"
    bars_state = (
        json.loads(bars_state_path.read_text(encoding="utf-8")) if bars_state_path.exists() else {}
    )
    failed_codes = bars_state.get("failed_codes", [])

    per_code_dates: dict[str, set[str]] = {}
    for code in candidate_codes:
        bars = load_bars(code)
        if bars is None:
            continue
        per_code_dates[code] = {r["Date"] for r in bars}

    # 参照営業日カレンダー: 全候補銘柄で観測された日付の和集合（契約全期間の実際の市場営業日の近似）
    reference_calendar: set[str] = set()
    for dates in per_code_dates.values():
        reference_calendar |= dates
    reference_calendar_sorted = sorted(reference_calendar)

    total_expected = 0
    total_actual = 0
    total_missing = 0
    per_code_detail = []
    for code, dates in per_code_dates.items():
        if not dates:
            continue
        code_min, code_max = min(dates), max(dates)
        expected_days = [d for d in reference_calendar_sorted if code_min <= d <= code_max]
        expected = len(expected_days)
        actual = len(dates)
        missing = expected - actual
        total_expected += expected
        total_actual += actual
        total_missing += missing
        per_code_detail.append(
            {
                "code": code,
                "active_range": [code_min, code_max],
                "expected_days": expected,
                "actual_days": actual,
                "missing_days": missing,
                "missing_rate": (missing / expected) if expected else None,
            }
        )

    overall_missing_rate = (total_missing / total_expected) if total_expected else None
    worst = sorted(per_code_detail, key=lambda r: -(r["missing_rate"] or 0))[:20]

    return {
        "candidate_codes_count": len(candidate_codes),
        "codes_with_bars_fetched": len(per_code_dates),
        "codes_fetch_failed_429_or_error": failed_codes,
        "reference_calendar_trading_days": len(reference_calendar_sorted),
        "reference_calendar_range": [
            reference_calendar_sorted[0] if reference_calendar_sorted else None,
            reference_calendar_sorted[-1] if reference_calendar_sorted else None,
        ],
        "total_expected_code_days": total_expected,
        "total_actual_code_days": total_actual,
        "total_missing_code_days": total_missing,
        "overall_missing_rate": overall_missing_rate,
        "worst_20_codes_by_missing_rate": worst,
        "methodology": (
            "各銘柄について、全候補銘柄の観測日付の和集合を参照営業日カレンダーとし、"
            "その銘柄自身の最初の観測日〜最後の観測日の範囲内にある参照営業日のうち、"
            "実際にその銘柄の行が存在しない日数を欠損とした（上場前/廃止後の期間は分母に含めない）。"
            "HTTP429で取得自体に失敗した銘柄は分母・分子いずれにも算入せず、"
            "codes_fetch_failed_429_or_error に別掲する（真の欠測と混同しないため）。"
        ),
    }


# ---------- SUE計算（9-3/9-4/9-5共通） ----------


def compute_all_sue_events(codes: list[str]) -> list[dict]:
    fins_records = load_fins_summary(codes_filter=set(codes))
    by_code = build_all_disclosures_index(fins_records)
    all_events = []
    for code, disclosures in by_code.items():
        all_events.extend(compute_raw_sue_for_code(disclosures))
    return all_events


# ---------- 9-3: 決算発表集中度 ----------


def task_9_3(all_events: list[dict], master_conf: list[dict]) -> dict:
    valid_events = [e for e in all_events if e["raw_sue"] is not None]
    by_date = Counter(e["disc_date"] for e in valid_events)
    n_days = len(by_date)
    counts = sorted(by_date.values())

    # 月別集計（決算シーズンの位置づけ確認用）
    by_month = Counter(d[:7] for d in by_date)

    # 3月決算比率: masterのCurFYEnではなく、fins側のCurFYEn（月=03）を使う
    fy_end_month_counter = Counter()
    for code, disclosures in build_all_disclosures_index(
        load_fins_summary(codes_filter={e["code"] for e in valid_events})
    ).items():
        for d in disclosures:
            fy_en = d.get("CurFYEn", "")
            if len(fy_en) >= 7:
                fy_end_month_counter[fy_en[5:7]] += 1

    days_with_ge5 = sum(1 for c in counts if c >= 5)

    return {
        "valid_event_count_all_periods": len(valid_events),
        "distinct_disclosure_days": n_days,
        "days_with_ge5_events": days_with_ge5,
        "events_per_day_distribution": {
            "min": counts[0] if counts else None,
            "median": counts[len(counts) // 2] if counts else None,
            "max": counts[-1] if counts else None,
            "mean": (sum(counts) / len(counts)) if counts else None,
        },
        "events_by_year_month": dict(sorted(by_month.items())),
        "cur_fy_end_month_distribution": dict(sorted(fy_end_month_counter.items())),
    }


# ---------- 9-4: データ十分性ゲート ----------


def task_9_4(all_events: list[dict], universe: dict, missing_rate: float | None) -> dict:
    confirmation_codes = set(universe["confirmation_universe"]["codes"])
    confirmation_final_count = universe["confirmation_universe"]["final_count"]

    valid_events = [e for e in all_events if e["raw_sue"] is not None]
    conf_events = [
        e
        for e in valid_events
        if e["code"] in confirmation_codes
        and CONFIRMATION_START.isoformat() <= e["disc_date"] <= CONFIRMATION_END.isoformat()
    ]
    ds1_count = len(conf_events)

    by_date = Counter(e["disc_date"] for e in conf_events)
    ds2_days = sum(1 for c in by_date.values() if c >= 5)

    ds3_count = confirmation_final_count

    ds4_missing_rate = missing_rate

    ds1_pass = ds1_count >= 300
    ds2_pass = ds2_days >= 30
    ds3_pass = ds3_count >= 150
    ds4_pass = (ds4_missing_rate is not None) and (ds4_missing_rate <= 0.05)

    all_pass = ds1_pass and ds2_pass and ds3_pass and ds4_pass

    return {
        "DS-1_confirmation_valid_event_count": ds1_count,
        "DS-1_threshold": 300,
        "DS-1_pass": ds1_pass,
        "DS-2_days_with_ge5_events": ds2_days,
        "DS-2_threshold": 30,
        "DS-2_pass": ds2_pass,
        "DS-3_confirmation_universe_count": ds3_count,
        "DS-3_threshold": 150,
        "DS-3_pass": ds3_pass,
        "DS-4_overall_missing_rate": ds4_missing_rate,
        "DS-4_threshold": 0.05,
        "DS-4_pass": ds4_pass,
        "all_pass": all_pass,
    }


# ---------- 9-5: RawSUE記述統計 ----------


def task_9_5(all_events: list[dict]) -> dict:
    valid = [e["raw_sue"] for e in all_events if e["raw_sue"] is not None]
    excluded_reasons = Counter(e["excluded_reason"] for e in all_events if e["excluded_reason"])

    if valid:
        s = sorted(valid)
        n = len(s)

        def pct(p):
            idx = min(n - 1, max(0, int(round(p * (n - 1)))))
            return s[idx]

        exact_zero = sum(1 for v in valid if v == 0.0)
        stats = {
            "n": n,
            "min": s[0],
            "p01": pct(0.01),
            "p05": pct(0.05),
            "p10": pct(0.10),
            "p25": pct(0.25),
            "median": pct(0.50),
            "p75": pct(0.75),
            "p90": pct(0.90),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "max": s[-1],
            "mean": sum(valid) / n,
            "exact_zero_count": exact_zero,
            "exact_zero_ratio": exact_zero / n,
        }
    else:
        stats = {"n": 0}

    return {
        "total_events_scanned": len(all_events),
        "valid_raw_sue_count": len(valid),
        "excluded_count_by_reason": dict(excluded_reasons),
        "raw_sue_descriptive_stats": stats,
    }


# ---------- params.json（spec固定パラメータの写し。9-1をここに含める） ----------


def build_params_json(task91: dict) -> dict:
    return {
        "spec_version_reference": "research/EXP-OBS000001/01-spec.md（2026-09-13初版）",
        "random_seed": 20260913,
        "field_value_mapping_9_1": task91,
        "sue_definition": {
            "formula": "RawSUE(j,t) = (A(t) - B(t)) / Scale(t)",
            "A_t_source": "1Q/2Q/3Q開示: FOdP / FY開示: OdP",
            "B_t_source": "直前開示が1Q/2Q/3Q/修正: FOdP / 直前がFY: NxFOdP",
            "Scale_t_source": "直前開示が1Q/2Q/3Q/修正: FSales / 直前がFY: NxFSales",
            "winsorize": "行わない（RawSUE・リターンとも）",
        },
        "prediction_window": {
            "horizon_primary_H": 5,
            "horizon_sensitivity": [3, 5, 10],
            "entry_price": "O(t1)（t0の翌営業日の始値）",
            "entry_slippage": 0.00075,
            "gross_return_formula": "R_H(j) = C(t_H) / O(t1) - 1",
        },
        "trailing_stop_params": {
            "initial_stop_pct": -0.03,
            "trail_activation_pct": 0.04,
            "trail_follow_pct": -0.02,
            "update_and_fill_basis": "日足終値のみで更新・抵触判定。抵触翌営業日始値で約定",
            "max_holding_business_days": 10,
            "forced_carry_over_max_days": 5,
        },
        "position_sizing": {
            "capital_jpy": 1_000_000,
            "target_position_jpy": 250_000,
            "position_cap_jpy": 375_000,
            "unit_shares": 100,
            "max_concurrent_positions": 5,
            "max_total_notional_jpy": 1_500_000,
            "max_same_sector_s33_positions": 2,
            "max_new_positions_same_disc_date": 3,
            "circuit_breaker_new_entry_halt_dd": 0.10,
            "circuit_breaker_liquidate_dd": 0.15,
        },
        "universe_rules": {
            "U1_scalecat": sorted(SCALECAT_U1_OK),
            "U2_mrgn_ok": sorted(MRGN_U2_OK),
            "U4_liquidity_min_va_jpy": 5e8,
            "U4_window_business_days": 60,
            "U5_price_band_main": [1000, 3500],
            "U6_max_universe": 175,
            "U7_min_universe": 150,
            "U7_price_band_fallback": [700, 8000],
        },
        "cost_model_summary": {
            "spread_roundtrip_pct": 0.0005,
            "slippage_roundtrip_pct": 0.0015,
            "commission_roundtrip_pct": 0.0005,
            "margin_interest_annual_pct": 0.028,
            "total_roundtrip_pct_at_avg_10cal_days": 0.0035,
            "status": "仮置き（R-2/R-3未確認）",
        },
        "japan_specific_conditions_status": {
            "A_lot_size_100shares": "実装（単元離散化・実効f記録）",
            "B_price_limit_UL_LL": (
                "実装（spec §4.0改訂により確定）。UL/LLは価格水準ではなく"
                "日中高値/安値が値幅制限に達したか否かの0/1フラグ。"
                "BUY_BLOCKED(t)=(Vo(t)==0) or (O(t) is null) or (UL(t)==1 and O(t)==H(t))、"
                "SELL_BLOCKED(t)=(Vo(t)==0) or (O(t) is null) or (LL(t)==1 and O(t)==L(t)) の2式で判定（§4.0.4）"
            ),
            "C_trading_hours": "実装（日足のO=寄付・C=大引けとして対応。日中足は使用しない）",
            "D_earnings_calendar_carryover_rule": "未実装（G2フェーズで実装予定。G1未達なら測定しない）",
            "E_margin_requirement_ratio": "未実装（G2フェーズで実装予定）",
            "F_credit_regulation": "模擬不能（C-5・HTTP403でデータ取得不可）。測定範囲の空白として明記",
            "G_margin_interest_rate": "未実装（G2フェーズで実装予定。年率2.8%×実保有暦日数/365）",
            "H_short_selling": "該当なし（ロングオンリー）",
        },
    }


def main() -> int:
    candidate_codes = json.loads((RESULT_DIR / "candidate_codes.json").read_text(encoding="utf-8"))["codes"]
    universe = json.loads((RESULT_DIR / "universe.json").read_text(encoding="utf-8"))

    print("9-1 実行中...")
    task91 = task_9_1()

    print("9-1b 実行中...")
    task91b = task_9_1b(candidate_codes)

    print("9-2 実行中...")
    task92 = task_9_2(candidate_codes)

    print("SUE計算中（9-3/9-4/9-5共通）...")
    all_events = compute_all_sue_events(candidate_codes)

    print("9-3 実行中...")
    task93 = task_9_3(all_events, None)

    print("9-4 実行中...")
    task94 = task_9_4(all_events, universe, task92["overall_missing_rate"])

    print("9-5 実行中...")
    task95 = task_9_5(all_events)

    params = build_params_json(task91)
    (RESULT_DIR / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    feasibility = {
        "9-1_summary": {
            "mapping_ambiguous": task91["mapping_ambiguous"],
            "note": "詳細は params.json の field_value_mapping_9_1 を参照",
        },
        "9-1b_ul_ll_flag_check": task91b,
        "9-2_missing_rate": task92,
        "9-3_concentration": task93,
        "9-4_data_sufficiency_gate": task94,
        "9-5_raw_sue_descriptive_stats": task95,
    }
    (RESULT_DIR / "feasibility.json").write_text(
        json.dumps(feasibility, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"saved: {RESULT_DIR / 'params.json'}")
    print(f"saved: {RESULT_DIR / 'feasibility.json'}")
    print(json.dumps(task94, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
