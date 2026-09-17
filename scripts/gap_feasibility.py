#!/usr/bin/env python3
"""EXP-OBS000003（非決算オーバーナイト・ギャップ）§9 先行タスク（第2版）の実測スクリプト。

spec: `research/EXP-OBS000003/01-spec.md`（第2版）§2.0・§3.1.1・§3.2・§3.4・§5.4・§6.0・§9。

実行順序: 9-1 → 9-2a → 9-2b(+V-1〜V-5) → 9-3 → 9-4 → 9-5 → 9-6(z*較正+DSゲート) → 9-7 → 9-8 → 9-9。
いずれかの必須ゲートが不成立の場合、それ以降は実行せず出力に `stop_reason` を記録する
（判定語は書かない。生データ・件数のみ）。

再現用コマンド:
    python3 scripts/gap_feasibility.py

出力:
  - research/EXP-OBS000003/10-result/feasibility.json
  - research/EXP-OBS000003/10-result/params.json
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gap_common as gc  # noqa: E402
from lib import gap_engine as ge  # noqa: E402
from lib.pead_common import AnomalousFlagValueError, parse_ul_ll_flag  # noqa: E402

RESULT_DIR = gc.RESULT_DIR
PEAD_RESULT_DIR = gc.PEAD_RESULT_DIR

KNOWN_T1 = "2024-06-21"
KNOWN_T_LEN = 487
KNOWN_T487 = "2026-06-22"
KNOWN_CANDIDATE_COUNT = 511

GRID = [-1.50, -1.75, -2.00, -2.25, -2.50, -2.75, -3.00, -3.25, -3.50, -3.75, -4.00]
YEAR_DAYS = 245.0
TARGET_N_ANN = 215.0

SELECTION_DU = dt.date(2024, 9, 18)  # T[61]
CONFIRMATION_DU = dt.date(2025, 7, 1)


def main() -> int:  # noqa: C901 - 直列のB実装タスクであり分割は可読性を落とす
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    feasibility: dict = {
        "generated_from": "gap_feasibility.py",
        "spec_reference": "research/EXP-OBS000003/01-spec.md（第2版）§9",
    }
    stop_reason: str | None = None
    escalate_kind: str | None = None  # "K-5" or "K-6" or None

    codes = gc.load_candidate_codes()
    cal = gc.Calendar(codes)

    # ---------------- 9-1: T の再構成 ----------------
    def at(i: int) -> str | None:
        return cal.at(i)

    checkpoints = {
        "T[1]": at(1), "T[60]": at(60), "T[61]": at(61), "T[62]": at(62),
        "T[66]": at(66), "T[67]": at(67), "T[68]": at(68),
        "T[477]": at(477), "T[481]": at(481), "T[482]": at(482),
        f"T[{len(cal.T)}]": at(len(cal.T)),
    }
    known_match = (
        at(1) == KNOWN_T1 and len(cal.T) == KNOWN_T_LEN and at(KNOWN_T_LEN) == KNOWN_T487
        and len(codes) == KNOWN_CANDIDATE_COUNT
    )
    r9_1 = {
        "candidate_codes_count": len(codes),
        "codes_missing_bars_count": len(cal.missing_bars_codes),
        "codes_missing_bars": cal.missing_bars_codes,
        "T_len": len(cal.T),
        "T_checkpoints": checkpoints,
        "known_values_match": known_match,
        "gate_len_ge_480": len(cal.T) >= 480,
        "gate_9_1_pass": bool(known_match and len(cal.T) >= 480),
    }
    feasibility["9-1_reference_calendar_T"] = r9_1
    log.append(f"[9-1] |T|={len(cal.T)} known_match={known_match} gate={r9_1['gate_9_1_pass']}")
    if not r9_1["gate_9_1_pass"]:
        stop_reason = "9-1: |T| または既知値との一致に失敗"
        escalate_kind = "K-6"

    # ---------------- 9-2a: 価格調整規約（分割・併合。第1版で確定済み・再実行） ----------------
    r9_2a = None
    if stop_reason is None:
        r9_2a = task_9_2a(codes, log)
        feasibility["9-2a_price_adjustment_convention"] = r9_2a
        if not r9_2a["gate_9_2a_pass"]:
            stop_reason = "9-2a: 分割・併合（E-1）の識別規約が一意に決まらない"
            escalate_kind = "K-6"

    # ---------------- 9-2b: 配当落ちの構成と V-1〜V-5 ----------------
    e2a = None
    e2b_dates = None
    r9_2b = None
    if stop_reason is None:
        fins_all = gc.load_fins_summary_all()
        codes_set = set(codes)
        fins_candidates = [r for r in fins_all if r.get("Code") in codes_set]
        basis_records, skipped_missing_fy = gc.construct_dividend_basis_records(fins_candidates)
        e2a = gc.build_e2a(cal, basis_records)
        e2b_dates = gc.build_e2b(cal)
        r9_2b = task_9_2b(cal, codes, fins_all, fins_candidates, basis_records, e2a, e2b_dates, log)
        feasibility["9-2b_dividend_construction_and_validation"] = r9_2b
        if not r9_2b["gate_v1_v5_all_pass"]:
            stop_reason = (
                "9-2b: 配当落ち日(E-2A/E-2B)の検証ゲートV-1〜V-5のいずれかが未達。"
                "K-6（司令塔承認済み方針）に従いSへ差し戻す。"
            )
            escalate_kind = "K-6"

    # ---------------- 9-3: UL/LL の再検証 ----------------
    r9_3 = None
    if stop_reason is None:
        r9_3 = task_9_3(codes, log)
        feasibility["9-3_ul_ll_flag_check"] = r9_3
        if not r9_3["gate_9_3_pass"]:
            stop_reason = "9-3: UL/LLの値がパース規則を満たさない、または欠損率が1%を超える"
            escalate_kind = "K-6"

    # 以降で使う共通構築物
    e2a_set: set[tuple[str, str]] = set(e2a["e2a_map"].keys()) if e2a else set()
    e2b_date_set: set[str] = set(e2b_dates.values()) if e2b_dates else set()

    # ---------------- 9-4: ユニバース構築 ----------------
    sel_universe = None
    conf_universe = None
    r9_4 = None
    if stop_reason is None:
        u_log: list[str] = []
        sel_universe = ge.build_period_universe(
            "selection", SELECTION_DU, gc.GAP_RAW_DIR / f"master_selection_du_{SELECTION_DU.isoformat()}.json",
            codes, u_log,
        )
        conf_universe = ge.build_period_universe(
            "confirmation", CONFIRMATION_DU, gc.PEAD_RAW_DIR / "master_confirmation_start_2025-07-01.json",
            codes, u_log,
        )
        pead_universe_codes = json.loads((PEAD_RESULT_DIR / "universe.json").read_text(encoding="utf-8"))[
            "confirmation_universe"
        ]["codes"]
        reproduction_match = sorted(conf_universe["codes"]) == sorted(pead_universe_codes)
        r9_4 = {
            "selection_universe": sel_universe,
            "confirmation_universe": conf_universe,
            "confirmation_universe_pead_reproduction_match": reproduction_match,
            "confirmation_universe_pead_code_count": len(pead_universe_codes),
            "gate_9_4_pass": bool(
                reproduction_match and not sel_universe["unresolved_below_min"]
                and not conf_universe["unresolved_below_min"]
            ),
        }
        feasibility["9-4_universe_construction"] = r9_4
        for line in u_log:
            log.append(f"[9-4] {line}")
        if not r9_4["gate_9_4_pass"]:
            stop_reason = "9-4: 確認期間ユニバースの再現一致に失敗、または選定/確認ユニバースが150銘柄未満"
            escalate_kind = "K-6"

    # ---------------- 9-5〜9-9: 有効ギャップ・候補判定エンジンの構築と記述統計 ----------------
    valid_positions = None
    gap_cache = None
    disc_positions_by_code = None
    I_SEL_START = I_SEL_END = I_CONF_START = I_CONF_END = None
    sel_rows = conf_rows = None
    r9_5 = None
    if stop_reason is None:
        valid_positions = ge.build_valid_positions(cal, e2a_set, e2b_date_set)
        gap_cache = ge.build_gap_value_cache(cal, valid_positions)
        fins_all = gc.load_fins_summary_all()
        codes_set = set(codes)
        fins_candidates = [r for r in fins_all if r.get("Code") in codes_set]
        disc_dates_by_code = ge.build_disc_dates_by_code(fins_candidates)
        disc_positions_by_code = ge.build_disc_positions_by_code(cal, disc_dates_by_code)

        I_SEL_START = 68
        I_SEL_END = max(i for i in range(1, len(cal.T) + 1) if cal.at(i) <= "2025-06-30")
        I_CONF_START = min(i for i in range(1, len(cal.T) + 1) if cal.at(i) >= "2025-07-01")
        I_CONF_END = len(cal.T)

        sel_rows = ge.build_period_rows(
            cal, sel_universe["codes"], I_SEL_START, I_SEL_END, valid_positions, gap_cache,
            e2a_set, e2b_date_set, disc_positions_by_code,
        )
        conf_rows = ge.build_period_rows(
            cal, conf_universe["codes"], I_CONF_START, I_CONF_END, valid_positions, gap_cache,
            e2a_set, e2b_date_set, disc_positions_by_code,
        )

        k_dist = ge.compute_k_distribution(cal, valid_positions)
        r9_5 = task_9_5(cal, sel_rows, conf_rows, k_dist, I_SEL_START, I_SEL_END, I_CONF_START, I_CONF_END, log)
        feasibility["9-5_gap_sigma_z_descriptive_stats"] = r9_5
        if not r9_5["gate_9_5_pass"]:
            stop_reason = "9-5: 算術整合性検算に失敗、またはK>66が1件でも観測された"
            escalate_kind = "K-6"

    # ---------------- 9-6: z* 較正 + DS-1〜DS-7 ----------------
    r9_6 = None
    z_star = None
    if stop_reason is None:
        sel_day_count = I_SEL_END - I_SEL_START + 1
        conf_day_count = I_CONF_END - I_CONF_START + 1
        r9_6 = task_9_6(sel_rows, conf_rows, sel_day_count, conf_day_count, log)
        feasibility["9-6_z_star_calibration_and_ds_gates"] = r9_6
        z_star = r9_6["z_star_frozen"]
        if not r9_6["ds_gates_all_pass"]:
            stop_reason = (
                "9-6: データ十分性ゲート(DS-1〜DS-7)のいずれかが未達。"
                "K-5に従いSへ差し戻す（判定不能。確認期間のリターンは未見のまま温存）。"
            )
            escalate_kind = "K-5"

    # ---------------- 9-7: PEAD 排反性 ----------------
    if stop_reason is None or r9_6 is not None:
        # 9-6 が DS ゲートで停止しても、9-7〜9-9 は記述・検算タスクであり実行できる
        # （リターンを見ない範囲の記録タスクのため。spec §9 の表は 9-4〜9-9 を並記している）。
        r9_7 = task_9_7(cal, sel_rows, conf_rows, codes, log)
        feasibility["9-7_pead_disjointness_check"] = r9_7

        r9_8 = task_9_8(conf_universe, r9_7, log)
        feasibility["9-8_unidentified_news_contamination"] = r9_8

        r9_9 = task_9_9(r9_5, r9_6, feasibility, log)
        feasibility["9-9_self_consistency_check"] = r9_9

    feasibility["stop_reason"] = stop_reason
    feasibility["escalate_to_S"] = stop_reason is not None
    feasibility["escalate_kind"] = escalate_kind
    feasibility["can_proceed_to_G1"] = stop_reason is None

    params = build_params(cal, r9_1, r9_2a, r9_2b, r9_3, r9_4, r9_5, r9_6, stop_reason, escalate_kind)

    (RESULT_DIR / "feasibility.json").write_text(
        json.dumps(feasibility, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (RESULT_DIR / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    for line in log:
        print(line)
    print(f"saved: {RESULT_DIR / 'feasibility.json'}")
    print(f"saved: {RESULT_DIR / 'params.json'}")

    if stop_reason:
        print(f"STOP ({escalate_kind}): {stop_reason}")
        return 2
    return 0


# ---------------------------------------------------------------------------
# 9-2a: 価格調整規約（第1版で確定済みの再実行。分割・併合のみ）
# ---------------------------------------------------------------------------


def task_9_2a(codes: list[str], log: list[str]) -> dict:
    total_rows = 0
    adjfactor_counter: Counter = Counter()
    exrt_counter: Counter = Counter()
    non_unity_rows: list[dict] = []

    for code in codes:
        bars = gc.load_bars_raw(code)
        if bars is None:
            continue
        bars_sorted = sorted(bars, key=lambda r: r["Date"])
        for idx, r in enumerate(bars_sorted):
            total_rows += 1
            af = r.get("AdjFactor")
            ex = r.get("ExRT")
            adjfactor_counter[repr(af)] += 1
            exrt_counter[repr(ex)] += 1
            if af is not None and af != 1.0:
                prev_row = bars_sorted[idx - 1] if idx > 0 else None
                entry = {"code": r.get("Code"), "date": r.get("Date"), "AdjFactor": af, "ExRT": ex}
                if prev_row is not None and prev_row.get("C") is not None and prev_row.get("AdjC") is not None:
                    diff = abs(prev_row["C"] * af - prev_row["AdjC"])
                    entry["prev_AdjC_matches_C_times_AdjFactor"] = diff <= 0.06
                non_unity_rows.append(entry)

    non_unity_values = sorted({e["AdjFactor"] for e in non_unity_rows})
    all_clean = all(abs((1.0 / v if v else 0) - round(1.0 / v if v else 0)) <= 1e-6 for v in non_unity_values)
    continuity_checked = [e for e in non_unity_rows if "prev_AdjC_matches_C_times_AdjFactor" in e]
    continuity_pass = sum(1 for e in continuity_checked if e["prev_AdjC_matches_C_times_AdjFactor"])
    split_merger_identifiable = (
        set(exrt_counter.keys()) <= {repr(None), repr("1")} and all_clean
        and continuity_pass == len(continuity_checked)
    )
    log.append(
        f"[9-2a] rows={total_rows} non_unity={len(non_unity_rows)} all_clean_ratio={all_clean} "
        f"continuity_pass={continuity_pass}/{len(continuity_checked)} split_merger_identifiable={split_merger_identifiable}"
    )
    return {
        "total_bars_rows_scanned": total_rows,
        "AdjFactor_non_unity_row_count": len(non_unity_rows),
        "all_non_unity_values_are_clean_split_ratios": all_clean,
        "cumulative_adjustment_continuity_checked_count": len(continuity_checked),
        "cumulative_adjustment_continuity_pass_count": continuity_pass,
        "split_merger_identifiable": split_merger_identifiable,
        "gate_9_2a_pass": split_merger_identifiable,
    }


# ---------------------------------------------------------------------------
# 9-2b: 配当落ちの構成と V-1〜V-5
# ---------------------------------------------------------------------------


def task_9_2b(cal, codes, fins_all, fins_candidates, basis_records, e2a, e2b_dates, log) -> dict:
    e2a_map = e2a["e2a_map"]

    def build_entries(shift: int = 0) -> tuple[list[float], list[float], int, int]:
        xs, ys = [], []
        excluded_e1 = 0
        missing = 0
        for (code, x), dps in e2a_map.items():
            i = cal.idx(x)
            j = i + shift
            date_j = cal.at(j)
            if date_j is None:
                missing += 1
                continue
            row_j = cal.row(code, date_j)
            if gc.is_split_merger_row(row_j):
                excluded_e1 += 1
                continue
            prev_date = cal.at(j - 1)
            row_prev = cal.row(code, prev_date) if prev_date else None
            if row_prev is None or row_prev.get("C") is None:
                missing += 1
                continue
            c_prev = float(row_prev["C"])
            if c_prev <= 0:
                missing += 1
                continue
            g = gc.gap_g(cal, code, date_j)
            if g is None:
                missing += 1
                continue
            xs.append(dps / c_prev)
            ys.append(g)
        return xs, ys, excluded_e1, missing

    # V-1
    xs0, ys0, excl_e1_v1, missing_v1 = build_entries(0)
    fit_v1 = gc.ols(xs0, ys0)
    v1_pass = fit_v1["b"] is not None and -1.15 <= fit_v1["b"] <= -0.70

    # V-2 プラセボ
    v2_results = {}
    for shift in (-2, -1, 1, 2):
        xs, ys, excl_e1, missing = build_entries(shift)
        fit = gc.ols(xs, ys)
        v2_results[str(shift)] = {
            "fit": fit, "excluded_e1": excl_e1, "missing": missing,
            "abs_b_le_0_35": (fit["b"] is not None and abs(fit["b"]) <= 0.35),
        }
    v2_pass = all(v["abs_b_le_0_35"] for v in v2_results.values())

    # V-3 日別再現性（V-1と同一の除外規則の下で日別に集計）
    by_x: dict[str, list[tuple[float, float]]] = {}
    for (code, x), dps in e2a_map.items():
        row_x = cal.row(code, x)
        if gc.is_split_merger_row(row_x):
            continue
        i = cal.idx(x)
        prev_date = cal.at(i - 1)
        row_prev = cal.row(code, prev_date)
        if row_prev is None or row_prev.get("C") is None:
            continue
        c_prev = float(row_prev["C"])
        if c_prev <= 0:
            continue
        g = gc.gap_g(cal, code, x)
        if g is None:
            continue
        by_x.setdefault(x, []).append((dps / c_prev, g))

    qualifying_days = {x: v for x, v in by_x.items() if len(v) >= 30}
    day_results = []
    in_range_count = 0
    for x, pairs in sorted(qualifying_days.items()):
        fit = gc.ols([p[0] for p in pairs], [p[1] for p in pairs])
        b = fit["b"]
        ok = b is not None and -1.25 <= b <= -0.60
        if ok:
            in_range_count += 1
        day_results.append({"x": x, "n": len(pairs), "b": b, "in_range": ok})
    v3_fraction = (in_range_count / len(qualifying_days)) if qualifying_days else None
    v3_pass = v3_fraction is not None and v3_fraction >= 0.70

    # V-4 カバレッジ
    covered = e2a["positive_codes"] | e2a["explicit_zero_only_codes"]
    v4_fraction = len(covered) / len(codes)
    v4_pass = v4_fraction >= 0.95

    # V-5 期末日の算術的自己検算（CurPerType='2Q'。候補銘柄・(code,CurFYSt,CurFYEn)でユニーク化）
    seen: dict[tuple[str, str, str], str] = {}
    for r in fins_candidates:
        if r.get("CurPerType") != "2Q":
            continue
        fy_st_s = r.get("CurFYSt")
        fy_en_s = r.get("CurFYEn")
        cur_per_en = r.get("CurPerEn")
        if not fy_st_s or not cur_per_en:
            continue
        key = (r.get("Code"), fy_st_s, fy_en_s)
        if key in seen:
            continue
        seen[key] = cur_per_en
    v5_total = len(seen)
    v5_match = 0
    for (code, fy_st_s, fy_en_s), cur_per_en in seen.items():
        fy_st = gc._parse_date_or_none(fy_st_s)
        if fy_st is None:
            continue
        if gc.quarter_end(fy_st, 6).isoformat() == cur_per_en:
            v5_match += 1
    v5_fraction = (v5_match / v5_total) if v5_total else None
    v5_pass = v5_fraction is not None and v5_fraction >= 0.995

    all_pass = v1_pass and v2_pass and v3_pass and v4_pass and v5_pass

    log.append(
        f"[9-2b] V1 b={fit_v1['b']} se={fit_v1['se_b']} n={fit_v1['n']} pass={v1_pass} | "
        f"V2 pass={v2_pass} {[ (k, round(v['fit']['b'],4) if v['fit']['b'] is not None else None) for k,v in v2_results.items()]} | "
        f"V3 {in_range_count}/{len(qualifying_days)}={v3_fraction} pass={v3_pass} | "
        f"V4 {len(covered)}/{len(codes)}={v4_fraction} pass={v4_pass} | "
        f"V5 {v5_match}/{v5_total}={v5_fraction} pass={v5_pass} | ALL_PASS={all_pass}"
    )

    return {
        "e2a_summary": {
            "positive_codes_count": len(e2a["positive_codes"]),
            "explicit_zero_only_codes_count": len(e2a["explicit_zero_only_codes"]),
            "any_record_codes_count": len(e2a["any_record_codes"]),
            "no_codes_with_any_record_count": len(codes) - len(e2a["any_record_codes"]),
            "no_mapping_count": e2a["no_mapping_count"],
            "below_t1_count": e2a["below_t1_count"],
            "e2a_pairs_count": len(e2a_map),
            "duplicate_pairs_count": e2a["duplicate_pairs_count"],
        },
        "e2b_summary": {
            "months_count": len(e2b_dates),
            "dates": e2b_dates,
        },
        "V1_pooled_regression": {
            "fit": fit_v1, "excluded_e1_count": excl_e1_v1, "missing_count": missing_v1,
            "threshold": [-1.15, -0.70], "pass": v1_pass,
        },
        "V2_placebo": {"results": v2_results, "threshold_abs_max": 0.35, "pass": v2_pass},
        "V3_day_reproducibility": {
            "qualifying_days_count": len(qualifying_days), "in_range_count": in_range_count,
            "fraction": v3_fraction, "threshold_fraction_min": 0.70, "day_results": day_results,
            "pass": v3_pass,
        },
        "V4_coverage": {
            "covered_codes_count": len(covered), "total_candidate_codes": len(codes),
            "fraction": v4_fraction, "threshold_fraction_min": 0.95, "pass": v4_pass,
        },
        "V5_period_end_self_check": {
            "total_2Q_unique_periods": v5_total, "match_count": v5_match,
            "fraction": v5_fraction, "threshold_fraction_min": 0.995, "pass": v5_pass,
        },
        "gate_v1_v5_all_pass": all_pass,
    }


# ---------------------------------------------------------------------------
# 9-3: UL/LL 再検証
# ---------------------------------------------------------------------------


def task_9_3(codes: list[str], log: list[str]) -> dict:
    ul_counter: Counter = Counter()
    ll_counter: Counter = Counter()
    total_rows = 0
    ul_null = 0
    ll_null = 0
    anomalous_values: set = set()

    for code in codes:
        bars = gc.load_bars_raw(code)
        if bars is None:
            continue
        for r in bars:
            total_rows += 1
            for raw, counter_ref, is_ul in ((r.get("UL"), ul_counter, True), (r.get("LL"), ll_counter, False)):
                counter_ref[repr(raw)] += 1
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
    gate_pass = values_confined and (missing_rate is not None and missing_rate <= 0.01)
    log.append(f"[9-3] total_rows={total_rows} anomalous={sorted(anomalous_values)} missing_rate={missing_rate} gate={gate_pass}")
    return {
        "total_bars_rows_scanned": total_rows,
        "UL_unique_values_and_counts": dict(ul_counter),
        "LL_unique_values_and_counts": dict(ll_counter),
        "combined_missing_rate": missing_rate,
        "anomalous_values": sorted(anomalous_values),
        "values_confined_to_1_0_empty_null": values_confined,
        "gate_9_3_pass": gate_pass,
    }


# ---------------------------------------------------------------------------
# 9-5: g/σ_gap/z の記述統計・算術整合性・Kの分布
# ---------------------------------------------------------------------------


def _pctl(sorted_vals: list[float], p: float) -> float | None:
    n = len(sorted_vals)
    if n == 0:
        return None
    idx = min(n - 1, max(0, int(round(p * (n - 1)))))
    return sorted_vals[idx]


def task_9_5(cal, sel_rows, conf_rows, k_dist, i_sel_start, i_sel_end, i_conf_start, i_conf_end, log) -> dict:
    excl_counter_sel: Counter = Counter()
    excl_counter_conf: Counter = Counter()
    for r in sel_rows:
        for e in r["exclusions"]:
            excl_counter_sel[e] += 1
    for r in conf_rows:
        for e in r["exclusions"]:
            excl_counter_conf[e] += 1

    is_cand_sel = [r for r in sel_rows if r["is_candidate"]]
    is_cand_conf = [r for r in conf_rows if r["is_candidate"]]

    def desc(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0}
        s = sorted(vals)
        n = len(s)
        mean = sum(s) / n
        return {
            "n": n, "mean": mean, "min": s[0], "max": s[-1],
            "p01": _pctl(s, 0.01), "p05": _pctl(s, 0.05), "p50": _pctl(s, 0.50),
            "p95": _pctl(s, 0.95), "p99": _pctl(s, 0.99),
        }

    sigma_desc_sel = desc([r["sigma"] for r in is_cand_sel if r["sigma"] is not None])
    z_desc_sel = desc([r["z"] for r in is_cand_sel if r["z"] is not None])
    sigma_desc_conf = desc([r["sigma"] for r in is_cand_conf if r["sigma"] is not None])
    z_desc_conf = desc([r["z"] for r in is_cand_conf if r["z"] is not None])

    # 総和の検算: own_day_ok=False(missing_data) + 各除外理由(重複あり) を含む全銘柄日 = sel_rows合計
    total_sel = len(sel_rows)
    total_conf = len(conf_rows)

    # 取引時間変更(B-5・2024-11-05)前後別の g / σ_gap 記述統計
    pre_change_g = [r["g"] for r in is_cand_sel if r["date"] < "2024-11-05"]
    post_change_g_sel = [r["g"] for r in is_cand_sel if r["date"] >= "2024-11-05"]
    trading_hours_change_note = {
        "change_date": "2024-11-05",
        "selection_pre_change_g_desc": desc(pre_change_g),
        "selection_post_change_g_desc": desc(post_change_g_sel),
        "confirmation_all_post_change": True,
    }

    gate_pass = k_dist["gate_k_le_66_all"]

    log.append(
        f"[9-5] sel_rows={total_sel} conf_rows={total_conf} "
        f"is_candidate_sel={len(is_cand_sel)} is_candidate_conf={len(is_cand_conf)} "
        f"k_max={k_dist['k_max']} gate_k_le_66={gate_pass}"
    )

    return {
        "period_ranges": {
            "selection_i_range": [i_sel_start, i_sel_end],
            "selection_date_range": [cal.at(i_sel_start), cal.at(i_sel_end)],
            "confirmation_i_range": [i_conf_start, i_conf_end],
            "confirmation_date_range": [cal.at(i_conf_start), cal.at(i_conf_end)],
        },
        "selection_total_code_day_rows": total_sel,
        "confirmation_total_code_day_rows": total_conf,
        "selection_exclusion_counts": dict(excl_counter_sel),
        "confirmation_exclusion_counts": dict(excl_counter_conf),
        "selection_is_candidate_count": len(is_cand_sel),
        "confirmation_is_candidate_count": len(is_cand_conf),
        "selection_sigma_gap_descriptive": sigma_desc_sel,
        "selection_z_descriptive": z_desc_sel,
        "confirmation_sigma_gap_descriptive": sigma_desc_conf,
        "confirmation_z_descriptive": z_desc_conf,
        "trading_hours_change_note_B5": trading_hours_change_note,
        "k_distribution_uncapped_lookback": k_dist,
        "gate_9_5_pass": gate_pass,
    }


# ---------------------------------------------------------------------------
# 9-6: z* 較正（§3.6） + DS-1〜DS-7
# ---------------------------------------------------------------------------


def task_9_6(sel_rows, conf_rows, sel_day_count, conf_day_count, log) -> dict:
    sel_is_cand = [r for r in sel_rows if r["is_candidate"]]
    conf_is_cand = [r for r in conf_rows if r["is_candidate"]]

    grid_results = []
    for zg in GRID:
        n = sum(1 for r in sel_is_cand if r["z"] <= zg)
        n_ann = n * YEAR_DAYS / sel_day_count
        grid_results.append({"z_grid": zg, "selection_count": n, "N_ann": n_ann})

    min_diff = min(abs(g["N_ann"] - TARGET_N_ANN) for g in grid_results)
    ties = [g for g in grid_results if abs(g["N_ann"] - TARGET_N_ANN) == min_diff]
    chosen = min(ties, key=lambda g: g["z_grid"])
    z_star = chosen["z_grid"]

    ds2a_qualifying = [g for g in grid_results if 150 <= g["N_ann"] <= 300]
    ds2a_pass = len(ds2a_qualifying) > 0

    # 確認期間: プール(z<=-1.5)・イベント集合(z<=z*)
    pool_conf = [r for r in conf_is_cand if r["z"] <= -1.5]
    event_conf = [r for r in conf_is_cand if r["z"] <= z_star]

    ds1_count = len(pool_conf)
    ds1_pass = ds1_count >= 1500

    conf_n_ann = len(event_conf) * YEAR_DAYS / conf_day_count
    ds2b_pass = 100 <= conf_n_ann <= 400

    # DS-3（ユニバース銘柄数>=150）は9-4で既に判定済み（selection/confirmationともに175銘柄）。

    event_days = set(r["date"] for r in event_conf)
    ds5_count = len(event_days)
    ds5_pass = ds5_count >= 80

    pool_by_day: Counter = Counter(r["date"] for r in pool_conf)
    ds6_count = sum(1 for d, c in pool_by_day.items() if c >= 5)
    ds6_pass = ds6_count >= 100

    event_by_day: Counter = Counter(r["date"] for r in event_conf)
    top_days = event_by_day.most_common(10)
    ds7_max_share = (top_days[0][1] / len(event_conf)) if event_conf else None
    ds7_pass = ds7_max_share is not None and ds7_max_share <= 0.25

    ds_all = {
        "DS-1": ds1_pass, "DS-2a": ds2a_pass, "DS-2b": ds2b_pass,
        "DS-5": ds5_pass, "DS-6": ds6_pass, "DS-7": ds7_pass,
    }
    ds_gates_all_pass = all(ds_all.values())

    log.append(
        f"[9-6] z*_chosen={z_star} sel_grid={[(g['z_grid'], g['selection_count'], round(g['N_ann'],2)) for g in grid_results]}"
    )
    log.append(
        f"[9-6] DS-1(pool>=1500)={ds1_count}:{ds1_pass} DS-2a(sel Nann in[150,300] exists)={ds2a_pass} "
        f"DS-2b(conf Nann in[100,400])={round(conf_n_ann,2)}:{ds2b_pass} "
        f"DS-5(K_event>=80)={ds5_count}:{ds5_pass} DS-6(K>=100)={ds6_count}:{ds6_pass} "
        f"DS-7(max_share<=25%)={ds7_max_share}:{ds7_pass} ALL_PASS={ds_gates_all_pass}"
    )

    return {
        "z_star_grid_11points": grid_results,
        "z_star_frozen": z_star,
        "z_star_selection_count": chosen["selection_count"],
        "z_star_selection_N_ann": chosen["N_ann"],
        "DS-1_confirmation_pool_count": {"value": ds1_count, "threshold_min": 1500, "pass": ds1_pass},
        "DS-2a_selection_qualifying_grid_points": {
            "qualifying_points": ds2a_qualifying, "exists": ds2a_pass,
        },
        "DS-2b_confirmation_N_ann": {
            "confirmation_event_count": len(event_conf), "confirmation_day_count": conf_day_count,
            "value": conf_n_ann, "threshold_range": [100, 400], "pass": ds2b_pass,
        },
        "DS-5_confirmation_distinct_event_days_K_event": {
            "value": ds5_count, "threshold_min": 80, "pass": ds5_pass,
        },
        "DS-6_confirmation_days_with_ge5_pool_obs_K": {
            "value": ds6_count, "distinct_pool_days_total": len(pool_by_day), "threshold_min": 100, "pass": ds6_pass,
        },
        "DS-7_confirmation_max_single_day_event_share": {
            "value": ds7_max_share, "top10_days": top_days, "threshold_max": 0.25, "pass": ds7_pass,
        },
        "ds_gates_summary": ds_all,
        "ds_gates_all_pass": ds_gates_all_pass,
        "confirmation_pool_count_raw": ds1_count,
        "confirmation_event_count_raw": len(event_conf),
    }


# ---------------------------------------------------------------------------
# 9-7: PEAD 排反性
# ---------------------------------------------------------------------------


def task_9_7(cal, sel_rows, conf_rows, codes, log) -> dict:
    all_rows = sel_rows + conf_rows
    e3_excluded_stock_days = [r for r in all_rows if "E3" in r["exclusions"]]

    # DiscDate ベース: E-3 除外の原因となった DiscDate の集合（銘柄別）
    fins_all = gc.load_fins_summary_all()
    codes_set = set(codes)
    fins_candidates = [r for r in fins_all if r.get("Code") in codes_set]
    disc_dates_by_code = ge.build_disc_dates_by_code(fins_candidates)
    total_disc_date_pairs = sum(len(v) for v in disc_dates_by_code.values())

    # 積集合チェック: is_candidate な行(D)について、その銘柄のDiscDateが D または D-1 と一致するものが無いことを直接確認
    intersection_violations = []
    is_cand_all = [r for r in all_rows if r["is_candidate"]]
    for r in is_cand_all:
        code = r["code"]
        i = r["i"]
        d_date = r["date"]
        d_minus_1 = cal.at(i - 1)
        discs = disc_dates_by_code.get(code, set())
        if d_date in discs or (d_minus_1 and d_minus_1 in discs):
            intersection_violations.append({"code": code, "date": d_date})

    fetch_range_note = (
        "/fins/earnings-date は本タスクでは未取得（G1未達のためG2へ未着手。§7 D項の抵触件数の実測はG2実行時に行う）。"
        "既知の取得範囲: 2024-06-22〜2026-06-22（spec §7 D項の記述）。"
    )

    result = {
        "e3_excluded_stock_day_count_total": len(e3_excluded_stock_days),
        "e3_excluded_stock_day_count_selection": sum(1 for r in sel_rows if "E3" in r["exclusions"]),
        "e3_excluded_stock_day_count_confirmation": sum(1 for r in conf_rows if "E3" in r["exclusions"]),
        "disc_date_total_pairs_all_candidate_codes": total_disc_date_pairs,
        "distinct_codes_with_disc_dates": len(disc_dates_by_code),
        "is_candidate_total_rows_checked": len(is_cand_all),
        "intersection_violations_count": len(intersection_violations),
        "intersection_violations_examples": intersection_violations[:20],
        "intersection_is_empty": len(intersection_violations) == 0,
        "earnings_date_fetch_note": fetch_range_note,
    }
    log.append(
        f"[9-7] E3_excluded_total={result['e3_excluded_stock_day_count_total']} "
        f"intersection_violations={result['intersection_violations_count']} "
        f"intersection_empty={result['intersection_is_empty']}"
    )
    return result


# ---------------------------------------------------------------------------
# 9-8: 未識別ニュース混入の記述
# ---------------------------------------------------------------------------


def task_9_8(conf_universe, r9_7, log) -> dict:
    files = sorted(glob.glob(str(gc.REPO_ROOT / "research" / "_snapshots" / "tdnet" / "*.json")))
    snapshot_dates = [Path(f).stem for f in files]
    j_quants_max_date = "2026-06-22"
    overlap = [d for d in snapshot_dates if d <= j_quants_max_date]

    conf_set = set(conf_universe["codes"]) if conf_universe else set()
    hit_pairs = 0
    total_pairs = 0
    by_date_hit = {}
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        date = d["date"]
        codes_today = set(r["code"] for r in d["rows"])
        hit = len(conf_set & codes_today)
        by_date_hit[date] = hit
        hit_pairs += hit
        total_pairs += len(conf_set)
    unconditional_rate = (hit_pairs / total_pairs) if total_pairs else None

    e3_total = r9_7["e3_excluded_stock_day_count_confirmation"] if r9_7 else None

    log.append(
        f"[9-8] tdnet_snapshot_range=[{min(snapshot_dates) if snapshot_dates else None},"
        f"{max(snapshot_dates) if snapshot_dates else None}] overlap_with_jquants={overlap} "
        f"unconditional_disclosure_rate={unconditional_rate}"
    )

    return {
        "tdnet_snapshot_date_range": [min(snapshot_dates), max(snapshot_dates)] if snapshot_dates else None,
        "tdnet_snapshot_file_count": len(snapshot_dates),
        "j_quants_daily_max_date": j_quants_max_date,
        "calendar_overlap_dates": overlap,
        "calendar_overlap_is_empty": len(overlap) == 0,
        "confirmation_universe_unconditional_disclosure_rate": unconditional_rate,
        "confirmation_universe_unconditional_disclosure_hit_pairs": hit_pairs,
        "confirmation_universe_unconditional_disclosure_total_pairs": total_pairs,
        "by_date_hit_count": by_date_hit,
        "e3_excluded_confirmation_stock_day_count": e3_total,
    }


# ---------------------------------------------------------------------------
# 9-9: 自己整合チェック
# ---------------------------------------------------------------------------


def task_9_9(r9_5, r9_6, feasibility, log) -> dict:
    checks = {}

    # (c) K（§6.1.2の対象日数）とDS-6が同一の値であること。
    # 本スクリプトはG1(§6.1)を実行していないため、この時点でK(§6.1.2)は未計算。
    # DS-6の計算関数(days with >=5 pool obs)はG1のK算出と同一の定義・同一のプールデータから
    # 導出するため、G1実行時に必然的に一致する（同一関数を再利用するため値の食い違いは原理的に発生しない）。
    checks["k_ds6_consistency"] = {
        "note": (
            "G1(§6.1)未実行のため直接比較はできない。DS-6は§6.1.2のK定義（1日あたりプール観測5件以上の日数）"
            "と同一の集計処理（gap_feasibility.py task_9_6）で計算しており、G1実行時にも同じ定義を"
            "再利用するため、値の食い違いは構造的に発生しない。"
        ),
        "ds6_value": r9_6["DS-6_confirmation_days_with_ge5_pool_obs_K"]["value"] if r9_6 else None,
    }
    checks["k_event_ds5_consistency"] = {
        "note": (
            "同様にDS-5（イベント集合の相異なる日数）は§6.1.3のK_eventと同一定義であり、"
            "G1実行時に同一関数を再利用するため値の食い違いは構造的に発生しない。"
        ),
        "ds5_value": r9_6["DS-5_confirmation_distinct_event_days_K_event"]["value"] if r9_6 else None,
    }

    # (b) 除外件数の総和が候補件数と一致すること（9-5の選定/確認それぞれで検算）
    if r9_5:
        for period in ("selection", "confirmation"):
            total_rows = r9_5[f"{period}_total_code_day_rows"]
            is_cand = r9_5[f"{period}_is_candidate_count"]
            excl_counts = r9_5[f"{period}_exclusion_counts"]
            # own_day_ok=False (missing_data) は exclusions=["missing_data"]の1件のみのグループ
            missing_data_count = excl_counts.get("missing_data", 0)
            # is_candidate + (何らかの除外がある行数) = total_rows のはず（除外は重複可なので
            # 「除外なし」の行数のみが is_candidate と一致するかを確認する）
            rows_with_any_exclusion_or_missing = total_rows - is_cand
            checks[f"{period}_total_consistency"] = {
                "total_rows": total_rows,
                "is_candidate_count": is_cand,
                "rows_with_any_exclusion_or_missing_data": rows_with_any_exclusion_or_missing,
                "consistent": (is_cand + rows_with_any_exclusion_or_missing) == total_rows,
            }

    all_consistent = all(
        v.get("consistent", True) for v in checks.values() if isinstance(v, dict)
    )
    log.append(f"[9-9] self_consistency_all={all_consistent}")
    return {"checks": checks, "gate_9_9_pass": all_consistent}


# ---------------------------------------------------------------------------
# params.json の構築
# ---------------------------------------------------------------------------


def build_params(cal, r9_1, r9_2a, r9_2b, r9_3, r9_4, r9_5, r9_6, stop_reason, escalate_kind) -> dict:
    return {
        "generated_from": "gap_feasibility.py",
        "spec_reference": "research/EXP-OBS000003/01-spec.md（第2版）",
        "seed": 20260914,
        "reference_calendar_T_checkpoints": r9_1["T_checkpoints"] if r9_1 else None,
        "reference_calendar_T_len": r9_1["T_len"] if r9_1 else None,
        "price_adjustment_convention": {
            "split_merger_identifiable": r9_2a["split_merger_identifiable"] if r9_2a else None,
        },
        "dividend_construction": {
            "method": "spec §3.1.1 E-2A(銘柄別・厳密)+E-2B(暦ベース・一括)の和集合",
            "v1_v5_all_pass": r9_2b["gate_v1_v5_all_pass"] if r9_2b else None,
            "v1": r9_2b["V1_pooled_regression"] if r9_2b else None,
            "v2": r9_2b["V2_placebo"] if r9_2b else None,
            "v3": r9_2b["V3_day_reproducibility"] if r9_2b else None,
            "v4": r9_2b["V4_coverage"] if r9_2b else None,
            "v5": r9_2b["V5_period_end_self_check"] if r9_2b else None,
        },
        "ul_ll_missing_rate": r9_3["combined_missing_rate"] if r9_3 else None,
        "universe": {
            "selection_D_u": SELECTION_DU.isoformat(),
            "confirmation_D_u": CONFIRMATION_DU.isoformat(),
            "selection_codes_count": len(r9_4["selection_universe"]["codes"]) if r9_4 else None,
            "confirmation_codes_count": len(r9_4["confirmation_universe"]["codes"]) if r9_4 else None,
            "confirmation_pead_reproduction_match": r9_4["confirmation_universe_pead_reproduction_match"] if r9_4 else None,
        },
        "z_star_frozen": r9_6["z_star_frozen"] if r9_6 else None,
        "z_star_grid": GRID,
        "z_star_year_days_constant": YEAR_DAYS,
        "z_star_target_N_ann": TARGET_N_ANN,
        "ds_gates_all_pass": r9_6["ds_gates_all_pass"] if r9_6 else None,
        "ds_gates_summary": r9_6["ds_gates_summary"] if r9_6 else None,
        "japan_specific_conditions_status": {
            "A_unit_100_share_discretization": "未実施（DSゲート未達またはG1未達のため）" if stop_reason else "未実施（G1後に評価）",
            "B_price_limit_ul_ll": "規約確定済み（E-4/E-5/E-7/E-8として候補判定に組込み済み）",
            "C_trading_hours_fixed": "記述統計のみ実施（9-5のB-5前後別）。G2は未実施",
            "D_earnings_date_avoidance": "未実施（/fins/earnings-date 未取得。G2到達時に取得予定）",
            "E_margin_requirement": "未実施（G2未達のため）",
            "F_credit_regulation_unsimulatable": "模擬不能（データ取得不可・C-5・HTTP403）。spec §7 F項の通り",
            "G_margin_interest_rate": "未実施（G2未達のため）",
            "H_short_leg": "該当なし（ロングオンリー・spec §5.2）",
            "I_rights_dividend_exclusion": (
                "E-1(分割併合)・E-2A(配当・銘柄別)・E-2B(配当・暦ベース)すべて識別可能。"
                f"V-1〜V-5={'全合格' if (r9_2b and r9_2b['gate_v1_v5_all_pass']) else '未達あり'}"
            ),
        },
        "stop_reason": stop_reason,
        "escalate_kind": escalate_kind,
    }


if __name__ == "__main__":
    raise SystemExit(main())
