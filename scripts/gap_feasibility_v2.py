#!/usr/bin/env python3
"""EXP-OBS000006（ギャップ・10年データ版）§9 先行タスク（第2版・§9-0〜§9-9）の実測スクリプト。

spec: `research/EXP-OBS000006/01-spec.md`（第2版）§2.0.1・§9。

**このスクリプトは `data/raw/` を一切書き込まない（読み取り専用）。** §9-0・N-15 により
既存の日足キャッシュ（`data/raw/pead/bars_daily/`）の上書き・再取得・削除は絶対に行わない。

実行順序: 9-0 → 9-1（読み込みのみ）→ T再構成・照合(契約範囲ピン留め) → 9-2 → 9-3(E-1/E-2A/E-2B+V-1〜V-5)
→ universe構築 → 9-4(遡り上限66再実測) → 9-5(T照合・n_sel_eff/n_conf_eff) → 9-6(z*較正+DSゲート)
→ 9-7(排反性) → 9-8(UL/LL) → 9-9(ローリング窓先頭欠け)。
いずれかの必須ゲートが不成立の場合、それ以降は実行せず `stop_reason` を記録する。

判定語は書かない。すべて実測値・件数のみ。

再現用コマンド:
    python3 scripts/gap_feasibility_v2.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gap_common as gc  # noqa: E402
from lib import gap_engine as ge  # noqa: E402
from lib import v2_common as v2  # noqa: E402
from lib.pead_common import AnomalousFlagValueError, parse_ul_ll_flag  # noqa: E402

RESULT_DIR = v2.GAP_RESULT_DIR
PEAD_RESULT_DIR = v2.PEAD_RESULT_DIR

GRID = [round(-1.50 - 0.25 * k, 2) for k in range(27)]  # -1.50 .. -8.00（27点）
YEAR_DAYS = 245.0
TARGET_N_ANN = 215.0

EXPECTED = {
    "N": 2441, "m": 1220, "T1": "2016-09-15", "TN": "2026-09-14",
    "Tm": "2021-09-15", "T61": "2016-12-15", "T68": "2016-12-27", "Tm_plus_1": "2021-09-16",
}


def main() -> int:  # noqa: C901
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    feasibility: dict = {
        "generated_from": "gap_feasibility_v2.py",
        "spec_reference": "research/EXP-OBS000006/01-spec.md（第2版）§9",
    }
    stop_reason: str | None = None
    escalate_kind: str | None = None

    # ---------------- 9-0: 破壊的操作の禁止・バックアップ確認（PEAD §9-0と共用可） ----------------
    backup = v2.verify_backup()
    feasibility["9-0_backup_verification"] = backup
    log.append(f"[9-0] backup_exists={backup['backup_exists']} path={backup['raw_data_backup_path']}")
    if not backup["backup_exists"]:
        stop_reason = "9-0: data/raw/ のバックアップが確認できない"
        escalate_kind = "K-6"

    # ---------------- 9-1: 候補集合（既存のcandidate_codes_v2.jsonを読み込むのみ。PEAD §9-1と共用） ----------------
    codes = v2.load_candidate_codes_v2()
    v2meta = json.loads((RESULT_DIR / "candidate_codes_v2.json").read_text(encoding="utf-8"))
    feasibility["9-1_candidate_set"] = {
        "candidate_codes_count": len(codes),
        "shared_with": "EXP-OBS000005 §9-1（同一のcandidate_codes_v2.json）",
        "selection_du_2016-12-15_pass_count": v2meta["selection_du_2016-12-15_pass_count"],
        "confirmation_du_2021-09-16_pass_count": v2meta["confirmation_du_2021-09-16_pass_count"],
    }
    log.append(f"[9-1] candidate_codes_count={len(codes)}")

    # ---------------- T の再構成（契約範囲ピン留め） ----------------
    cal = v2.CalendarV2(codes)
    N = len(cal.T)
    m = N // 2
    t_checkpoints = {
        "T[1]": cal.at(1), "T[N]": cal.at(N), "T[m]": cal.at(m), "T[m+1]": cal.at(m + 1),
        "T[61]": cal.at(61), "T[68]": cal.at(68),
        "T[m-11]": cal.at(m - 11), "T[N-11]": cal.at(N - 11),
    }
    known_match = (
        N == EXPECTED["N"] and m == EXPECTED["m"] and cal.at(1) == EXPECTED["T1"]
        and cal.at(N) == EXPECTED["TN"] and cal.at(m) == EXPECTED["Tm"]
        and cal.at(61) == EXPECTED["T61"] and cal.at(68) == EXPECTED["T68"]
        and cal.at(m + 1) == EXPECTED["Tm_plus_1"]
    )
    t_recon = {
        "contract_range_pin": [v2.CONTRACT_START, v2.CONTRACT_END],
        "N": N, "m": m, "missing_bars_codes": cal.missing_bars_codes,
        "checkpoints": t_checkpoints, "expected": EXPECTED, "matches_expected": known_match,
    }
    feasibility["T_reconstruction"] = t_recon
    log.append(f"[T] N={N} m={m} T[1]={cal.at(1)} T[N]={cal.at(N)} T[68]={cal.at(68)} matches={known_match}")
    if stop_reason is None and not known_match:
        stop_reason = "T再構成がspec §2.0.1の期待値と一致しない"
        escalate_kind = "K-6"

    # ---------------- 9-2: AdjFactor/ExRT/Adj* 調整規約 ----------------
    r9_2 = None
    if stop_reason is None:
        r9_2 = task_9_2(codes, cal, log)
        feasibility["9-2_price_adjustment_convention"] = r9_2
        if not r9_2["gate_pass"]:
            stop_reason = "9-2: AdjFactor/ExRTの調整規約が一意に決まらない"
            escalate_kind = "K-6"

    # ---------------- 9-3: 配当落ち日構成 + V-1〜V-5 ----------------
    e2a = e2b_dates = r9_3 = None
    e2a_set: set[tuple[str, str]] = set()
    e2b_date_set: set[str] = set()
    if stop_reason is None:
        fins_all = gc.load_fins_summary_all()
        codes_set = set(codes)
        fins_candidates = [r for r in fins_all if r.get("Code") in codes_set]
        basis_records, skipped_missing_fy = gc.construct_dividend_basis_records(fins_candidates)
        e2a = gc.build_e2a(cal, basis_records)
        e2b_dates = gc.build_e2b(cal)
        e2a_set = set(e2a["e2a_map"].keys())
        e2b_date_set = set(e2b_dates.values())
        r9_3 = task_9_3(cal, codes, fins_candidates, basis_records, e2a, e2b_dates, skipped_missing_fy, log)
        feasibility["9-3_dividend_identification_gates"] = r9_3
        if not r9_3["gate_v1_v5_all_pass"]:
            stop_reason = "9-3: 配当落ち日(E-2A/E-2B)の検証ゲートV-1〜V-5のいずれかが未達。K-6に従いSへ差し戻す。"
            escalate_kind = "K-6"

    # ---------------- universe構築 ----------------
    sel_universe = conf_universe = None
    if stop_reason is None:
        u_log: list[str] = []
        sel_master_path = v2.PEAD_RAW_DIR / f"master_d_sel_v2_{v2.D_SEL_V2.isoformat()}.json"
        conf_master_path = v2.PEAD_RAW_DIR / f"master_d_conf_v2_{v2.D_CONF_V2.isoformat()}.json"
        sel_universe = ge.build_period_universe("selection", v2.D_SEL_V2, sel_master_path, codes, u_log)
        conf_universe = ge.build_period_universe("confirmation", v2.D_CONF_V2, conf_master_path, codes, u_log)
        for line in u_log:
            log.append(f"[universe] {line}")
        (RESULT_DIR / "universe_v2.json").write_text(
            json.dumps({"selection_universe": sel_universe, "confirmation_universe": conf_universe},
                       ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if sel_universe["unresolved_below_min"] or conf_universe["unresolved_below_min"]:
            stop_reason = "universe: U-7緩和後も150銘柄以上のユニバースが成立しない"
            escalate_kind = "K-6"

    # ---------------- 有効ギャップ・候補判定エンジンの構築 ----------------
    valid_positions = gap_cache = disc_positions_by_code = None
    I_SEL_START = I_SEL_END = I_CONF_START = I_CONF_END = None
    sel_rows = conf_rows = None
    r9_4 = None
    if stop_reason is None:
        valid_positions = ge.build_valid_positions(cal, e2a_set, e2b_date_set)
        gap_cache = ge.build_gap_value_cache(cal, valid_positions)

        # 9-4: 遡り上限66の再実測（10年データ・v2候補集合）
        k_dist = ge.compute_k_distribution(cal, valid_positions)
        r9_4 = k_dist
        feasibility["9-4_lookback_66_recheck"] = r9_4
        log.append(f"[9-4] k_max={k_dist['k_max']} coverage_66={k_dist['k_le_66_coverage_rate']} gate={k_dist['gate_k_le_66_all']}")
        if not k_dist["gate_k_le_66_all"]:
            stop_reason = "9-4: 有効ギャップ60本収集に要する遡り営業日数が66を超える銘柄日が存在する"
            escalate_kind = "K-6"

    if stop_reason is None:
        fins_all = gc.load_fins_summary_all()
        codes_set = set(codes)
        fins_candidates = [r for r in fins_all if r.get("Code") in codes_set]
        disc_dates_by_code = ge.build_disc_dates_by_code(fins_candidates)
        disc_positions_by_code = ge.build_disc_positions_by_code(cal, disc_dates_by_code)

        I_SEL_START, I_SEL_END = 68, m - 11
        I_CONF_START, I_CONF_END = m + 1, N - 11

        sel_rows = ge.build_period_rows(
            cal, sel_universe["codes"], I_SEL_START, I_SEL_END, valid_positions, gap_cache,
            e2a_set, e2b_date_set, disc_positions_by_code,
        )
        conf_rows = ge.build_period_rows(
            cal, conf_universe["codes"], I_CONF_START, I_CONF_END, valid_positions, gap_cache,
            e2a_set, e2b_date_set, disc_positions_by_code,
        )

        # 9-5: n_sel_eff / n_conf_eff（E-2Bを除いた有効営業日数）とT照合
        n_sel_eff = sum(1 for i in range(I_SEL_START, I_SEL_END + 1) if cal.at(i) not in e2b_date_set)
        n_conf_eff = sum(1 for i in range(I_CONF_START, I_CONF_END + 1) if cal.at(i) not in e2b_date_set)
        r9_5 = {
            "selection_i_range": [I_SEL_START, I_SEL_END],
            "confirmation_i_range": [I_CONF_START, I_CONF_END],
            "selection_date_range": [cal.at(I_SEL_START), cal.at(I_SEL_END)],
            "confirmation_date_range": [cal.at(I_CONF_START), cal.at(I_CONF_END)],
            "n_sel_eff": n_sel_eff, "n_conf_eff": n_conf_eff,
            "t_checkpoints_match_spec_section_2_1": (
                cal.at(I_SEL_START) == "2016-12-27" and cal.at(I_CONF_END) == "2026-08-28"
            ),
        }
        feasibility["9-5_T_reconstruction_and_n_eff"] = r9_5
        log.append(f"[9-5] n_sel_eff={n_sel_eff} n_conf_eff={n_conf_eff} sel_range={r9_5['selection_date_range']} conf_range={r9_5['confirmation_date_range']}")
        if not r9_5["t_checkpoints_match_spec_section_2_1"]:
            stop_reason = "9-5: イベント日範囲がspec §2.1の表と一致しない"
            escalate_kind = "K-6"

    # ---------------- 9-6: z*較正 + DSゲート ----------------
    r9_6 = None
    z_star = None
    if stop_reason is None:
        r9_6 = task_9_6(sel_rows, conf_rows, n_sel_eff, n_conf_eff, log)
        feasibility["9-6_z_star_calibration_and_ds_gates"] = r9_6
        z_star = r9_6["z_star_frozen"]
        if not r9_6["ds_gates_all_pass"]:
            stop_reason = (
                "9-6: データ十分性ゲート(DS-1/DS-2a/DS-2b/DS-3/DS-4/DS-5/DS-6/DS-7/C7-3)のいずれかが未達。"
                "K-5に従いSへ差し戻す（判定不能。確認期間のリターンは未見のまま温存）。"
            )
            escalate_kind = "K-5"

    # ---------------- 9-7〜9-9: 記録タスク（DSゲート結果に関わらず実施） ----------------
    if sel_rows is not None:
        r9_7 = task_9_7(cal, sel_rows, conf_rows, codes, log)
        feasibility["9-7_pead_disjointness_and_exclusion_breakdown"] = r9_7

    r9_8 = task_9_8(codes, log)
    feasibility["9-8_ul_ll_flag_check"] = r9_8
    if stop_reason is None and not r9_8["gate_pass"]:
        stop_reason = "9-8: UL/LLの値がパース規則を満たさない、または欠損率が1%を超える"
        escalate_kind = "K-6"

    if stop_reason is None:
        r9_9 = task_9_9(cal, codes, log)
        feasibility["9-9_rolling_window_leading_gap"] = r9_9

    feasibility["stop_reason"] = stop_reason
    feasibility["escalate_to_S"] = stop_reason is not None
    feasibility["escalate_kind"] = escalate_kind
    feasibility["can_proceed_to_G1"] = stop_reason is None

    params = build_params(cal, t_recon, r9_2, r9_3, backup, r9_6, z_star)

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
# 9-2: AdjFactor/ExRT/Adj* 調整規約
# ---------------------------------------------------------------------------


def _is_clean_ratio(af: float) -> bool:
    """AdjFactorが小さい整数比（例: 1/5, 1/2, 5, 2, 6/5, 13/10 等）で表せるかを検査する。

    旧EXP-OBS000003の9-2aは「AdjFactor<1（順方向分割）のみ、1/AdjFactorが整数」という
    片側の検査しかしておらず、511銘柄・より狭い期間のデータではAdjFactor>1（逆方向＝併合・
    株式併合）が一度も出現しなかったため発覚しなかった。本関数はAdjFactor>1側（1/af ではなく
    af 自体が整数に近いか）も、非整数の小さい分数比（例: 1.2倍=6/5, 1.3倍=13/10 の値幅調整
    による株式分割）も許容するよう拡張している。
    """
    if af is None or af == 0:
        return False
    for denom in range(1, 41):
        val = af * denom
        if abs(val - round(val)) <= 1e-6 and 1 <= round(val) <= 200:
            return True
    return False


def task_9_2(codes: list[str], cal: v2.CalendarV2, log: list[str]) -> dict:
    total_rows = 0
    adjfactor_counter: Counter = Counter()
    exrt_counter: Counter = Counter()
    non_unity_rows: list[dict] = []
    non_unity_by_era: dict[str, Counter] = {"2016-2017": Counter(), "2021": Counter()}
    exrt2_continuity_ok = 0
    exrt2_continuity_checked = 0
    exrt1_continuity_ok = 0
    exrt1_continuity_checked = 0

    for code in codes:
        bd = cal.bars_by_code.get(code)
        if not bd:
            continue
        bars_sorted = sorted(bd.values(), key=lambda r: r["Date"])
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
                    ok = diff <= 0.06
                    entry["prev_AdjC_matches_C_times_AdjFactor"] = ok
                    if ex == "2":
                        exrt2_continuity_checked += 1
                        exrt2_continuity_ok += 1 if ok else 0
                    elif ex == "1":
                        exrt1_continuity_checked += 1
                        exrt1_continuity_ok += 1 if ok else 0
                non_unity_rows.append(entry)
                d = r["Date"]
                if d < "2018-01-01":
                    non_unity_by_era["2016-2017"][af] += 1
                elif "2021-01-01" <= d < "2022-01-01":
                    non_unity_by_era["2021"][af] += 1

    non_unity_values = sorted({e["AdjFactor"] for e in non_unity_rows})
    all_clean = all(_is_clean_ratio(v) for v in non_unity_values)
    non_clean_values = sorted(v for v in non_unity_values if not _is_clean_ratio(v))
    continuity_checked = [e for e in non_unity_rows if "prev_AdjC_matches_C_times_AdjFactor" in e]
    continuity_pass = sum(1 for e in continuity_checked if e["prev_AdjC_matches_C_times_AdjFactor"])

    # ExRT='1' のみを権利落ち（分割・併合）とみなす旧来の定義（gap_common.is_split_merger_row）
    # に対して、ExRT='2' が新たに実データで観測された（本10年版で初めて。旧EXP-OBS000003の
    # 511銘柄・より狭い期間のデータには一度も出現しなかった）。ExRT='2' の意味は
    # AdjFactor>1（逆方向の累積調整＝株式併合と整合的）かつ継続性検算が約69%成立する
    # という状況証拠はあるが、J-Quantsの公式フィールド定義を本セッションでは確認できておらず、
    # 既存のis_split_merger_row（E-1判定・σ_gap有効窓判定の両方に使う共有関数）が
    # ExRT='2'を捕捉しない状態のまま残っている。これは「一意に決まらない」に該当するため、
    # 本タスクは推測で処理方法を決めずSに差し戻す。
    exrt_values_seen = set(exrt_counter.keys()) - {repr(None)}
    exrt2_present = repr("2") in exrt_counter
    split_merger_identifiable = (exrt_values_seen <= {repr("1")}) and all_clean and (continuity_pass == len(continuity_checked))

    log.append(
        f"[9-2] rows={total_rows} non_unity={len(non_unity_rows)} all_clean={all_clean} "
        f"non_clean_values={non_clean_values} "
        f"continuity_pass={continuity_pass}/{len(continuity_checked)} "
        f"exrt2_present={exrt2_present} exrt2_continuity={exrt2_continuity_ok}/{exrt2_continuity_checked} "
        f"exrt1_continuity={exrt1_continuity_ok}/{exrt1_continuity_checked} "
        f"split_merger_identifiable={split_merger_identifiable}"
    )
    return {
        "field_names": ["AdjFactor", "ExRT", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo"],
        "total_bars_rows_scanned_pinned_range": total_rows,
        "AdjFactor_unique_values_and_counts": {str(k): v for k, v in adjfactor_counter.items()},
        "ExRT_unique_values_and_counts": {str(k): v for k, v in exrt_counter.items()},
        "AdjFactor_non_unity_row_count": len(non_unity_rows),
        "all_non_unity_values_are_clean_split_ratios": all_clean,
        "non_clean_adjfactor_values": non_clean_values,
        "cumulative_adjustment_continuity_checked_count": len(continuity_checked),
        "cumulative_adjustment_continuity_pass_count": continuity_pass,
        "era_check_non_unity_count_2016_2017": dict(non_unity_by_era["2016-2017"]),
        "era_check_non_unity_count_2021": dict(non_unity_by_era["2021"]),
        "EXRT_2_NEWLY_DISCOVERED_finding": {
            "description": (
                "ExRT='2' が本10年版・554銘柄データで初めて観測された（旧EXP-OBS000003の"
                "511銘柄・より狭い期間データには0件）。AdjFactorはExRT='2'の全100行で"
                "{2.0, 5.0, 10.0}のいずれか（>1、整数）であり、ExRT='1'（AdjFactor<1が通常）"
                "とは逆方向。継続性検算（prev_C×AdjFactor≒prev_AdjC）は69/100件で成立し、"
                "残り31件は主にprev_AdjCがまだprev_Cと同値（未反映）というパターンで不一致。"
                "既存のgap_common.is_split_merger_row()はExRT=='1'のみを分割・併合として"
                "扱っており、ExRT='2'を捕捉しない。この関数はE-1除外判定とσ_gap有効窓判定の"
                "両方に使う共有関数であり、EXP-OBS000003（frozen）とEXP-OBS000006（本EXP）の"
                "双方に影響する。ExRT='2'が何を表すか（株式併合か、それ以外か）についての"
                "公式フィールド定義をこのセッションでは確認できなかった。"
            ),
            "exrt2_row_count": exrt_counter.get(repr("2"), 0),
            "exrt2_distinct_codes": len({e["code"] for e in non_unity_rows if e.get("ExRT") == "2"}),
            "exrt2_adjfactor_values": sorted({e["AdjFactor"] for e in non_unity_rows if e.get("ExRT") == "2"}),
            "exrt2_continuity_check": {"pass": exrt2_continuity_ok, "checked": exrt2_continuity_checked},
            "exrt1_continuity_check": {"pass": exrt1_continuity_ok, "checked": exrt1_continuity_checked},
        },
        "convention": (
            "AdjFactorは累積調整係数。ExRT='1'は順方向の権利落ち（分割等、AdjFactor<1）を示す"
            "フラグとして既存実装で扱われている。ExRT='2'の意味は本タスクでは未確定（上記参照）。"
        ),
        "split_merger_identifiable": split_merger_identifiable,
        "gate_pass": split_merger_identifiable,
    }


# ---------------------------------------------------------------------------
# 9-3: 配当落ち日構成 + V-1〜V-5（gap_common.pyの既存ロジックを再利用）
# ---------------------------------------------------------------------------


def task_9_3(cal, codes, fins_candidates, basis_records, e2a, e2b_dates, skipped_missing_fy, log) -> dict:
    e2a_map = e2a["e2a_map"]

    def build_entries(shift: int = 0):
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

    xs0, ys0, excl_e1_v1, missing_v1 = build_entries(0)
    fit_v1 = gc.ols(xs0, ys0)
    v1_pass = fit_v1["b"] is not None and -1.15 <= fit_v1["b"] <= -0.70

    v2_results = {}
    for shift in (-2, -1, 1, 2):
        xs, ys, excl_e1, missing = build_entries(shift)
        fit = gc.ols(xs, ys)
        v2_results[str(shift)] = {
            "fit": fit, "excluded_e1": excl_e1, "missing": missing,
            "abs_b_le_0_35": (fit["b"] is not None and abs(fit["b"]) <= 0.35),
        }
    v2_pass = all(v["abs_b_le_0_35"] for v in v2_results.values())

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

    covered = e2a["positive_codes"] | e2a["explicit_zero_only_codes"]
    v4_fraction = len(covered) / len(codes)
    v4_pass = v4_fraction >= 0.95

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
        f"[9-3] V1 b={fit_v1['b']} n={fit_v1['n']} pass={v1_pass} | V2 pass={v2_pass} | "
        f"V3 {in_range_count}/{len(qualifying_days)}={v3_fraction} pass={v3_pass} | "
        f"V4 {len(covered)}/{len(codes)}={v4_fraction} pass={v4_pass} | "
        f"V5 {v5_match}/{v5_total}={v5_fraction} pass={v5_pass} | ALL_PASS={all_pass}"
    )

    return {
        "skipped_missing_fy_count": skipped_missing_fy,
        "e2a_summary": {
            "positive_codes_count": len(e2a["positive_codes"]),
            "explicit_zero_only_codes_count": len(e2a["explicit_zero_only_codes"]),
            "any_record_codes_count": len(e2a["any_record_codes"]),
            "no_codes_with_any_record_count": len(codes) - len(e2a["any_record_codes"]),
            "no_mapping_count": e2a["no_mapping_count"], "below_t1_count": e2a["below_t1_count"],
            "e2a_pairs_count": len(e2a_map), "duplicate_pairs_count": e2a["duplicate_pairs_count"],
        },
        "e2b_summary": {"months_count": len(e2b_dates)},
        "V1_pooled_regression": {"fit": fit_v1, "excluded_e1_count": excl_e1_v1, "missing_count": missing_v1,
                                  "threshold": [-1.15, -0.70], "pass": v1_pass},
        "V2_placebo": {"results": v2_results, "threshold_abs_max": 0.35, "pass": v2_pass},
        "V3_day_reproducibility": {"qualifying_days_count": len(qualifying_days), "in_range_count": in_range_count,
                                    "fraction": v3_fraction, "threshold_fraction_min": 0.70, "pass": v3_pass},
        "V4_coverage": {"covered_codes_count": len(covered), "total_candidate_codes": len(codes),
                        "fraction": v4_fraction, "threshold_fraction_min": 0.95, "pass": v4_pass},
        "V5_period_end_self_check": {"total_2Q_unique_periods": v5_total, "match_count": v5_match,
                                      "fraction": v5_fraction, "threshold_fraction_min": 0.995, "pass": v5_pass},
        "gate_v1_v5_all_pass": all_pass,
    }


# ---------------------------------------------------------------------------
# 9-6: z*較正 + DSゲート
# ---------------------------------------------------------------------------


def task_9_6(sel_rows, conf_rows, n_sel_eff, n_conf_eff, log) -> dict:
    sel_is_cand = [r for r in sel_rows if r["is_candidate"]]
    conf_is_cand = [r for r in conf_rows if r["is_candidate"]]

    grid_results = []
    for zg in GRID:
        n = sum(1 for r in sel_is_cand if r["z"] <= zg)
        n_ann = n * YEAR_DAYS / n_sel_eff
        grid_results.append({"z_grid": zg, "selection_count": n, "N_ann": n_ann})

    min_diff = min(abs(g["N_ann"] - TARGET_N_ANN) for g in grid_results)
    ties = [g for g in grid_results if abs(g["N_ann"] - TARGET_N_ANN) == min_diff]
    chosen = min(ties, key=lambda g: g["z_grid"])
    z_star = chosen["z_grid"]

    ds2a_qualifying = [g for g in grid_results if 150 <= g["N_ann"] <= 300]
    ds2a_pass = len(ds2a_qualifying) > 0

    pool_conf = [r for r in conf_is_cand if r["z"] <= -1.5]
    event_conf = [r for r in conf_is_cand if r["z"] <= z_star]
    pool_sel = [r for r in sel_is_cand if r["z"] <= -1.5]
    event_sel = [r for r in sel_is_cand if r["z"] <= z_star]

    ds1_count = len(pool_conf)
    ds1_pass = ds1_count >= 1500

    conf_n_ann = len(event_conf) * YEAR_DAYS / n_conf_eff
    ds2b_pass = 100 <= conf_n_ann <= 400

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

    rate_sel = len(event_sel) / n_sel_eff if n_sel_eff else None
    rate_conf = len(event_conf) / n_conf_eff if n_conf_eff else None
    c7_3_ratio = (max(rate_sel, rate_conf) / min(rate_sel, rate_conf)) if (rate_sel and rate_conf) else None
    c7_3_pass = c7_3_ratio is not None and c7_3_ratio <= 2.0

    # DS-3/DS-4 はユニバース構築・9-3で別途評価済み（両方とも合格前提でここに到達）。
    # 記録専用として True を明示する（このスクリプト内では universe/9-3ゲートが不合格なら
    # ここまで到達しない構造になっている）。
    ds3_pass = True
    ds4_pass = True

    ds_all = {
        "DS-1": ds1_pass, "DS-2a": ds2a_pass, "DS-2b": ds2b_pass, "DS-3": ds3_pass, "DS-4": ds4_pass,
        "DS-5": ds5_pass, "DS-6": ds6_pass, "DS-7": ds7_pass, "C7-3": c7_3_pass,
    }
    ds_gates_all_pass = all(ds_all.values())

    log.append(
        f"[9-6] z*_chosen={z_star} DS-1={ds1_count}:{ds1_pass} DS-2a={ds2a_pass} "
        f"DS-2b={round(conf_n_ann,2)}:{ds2b_pass} DS-5={ds5_count}:{ds5_pass} DS-6={ds6_count}:{ds6_pass} "
        f"DS-7={ds7_max_share}:{ds7_pass} C7-3={c7_3_ratio}:{c7_3_pass} ALL_PASS={ds_gates_all_pass}"
    )

    return {
        "z_star_grid_27points": grid_results,
        "z_star_frozen": z_star,
        "z_star_selection_count": chosen["selection_count"], "z_star_selection_N_ann": chosen["N_ann"],
        "DS-1_confirmation_pool_count": {"value": ds1_count, "threshold_min": 1500, "pass": ds1_pass},
        "DS-2a_selection_qualifying_grid_points": {"qualifying_points": ds2a_qualifying, "exists": ds2a_pass},
        "DS-2b_confirmation_N_ann": {"confirmation_event_count": len(event_conf), "confirmation_n_eff": n_conf_eff,
                                      "value": conf_n_ann, "threshold_range": [100, 400], "pass": ds2b_pass},
        "DS-5_confirmation_distinct_event_days_K_event": {"value": ds5_count, "threshold_min": 80, "pass": ds5_pass},
        "DS-6_confirmation_days_with_ge5_pool_obs_K": {"value": ds6_count, "distinct_pool_days_total": len(pool_by_day),
                                                        "threshold_min": 100, "pass": ds6_pass},
        "DS-7_confirmation_max_single_day_event_share": {"value": ds7_max_share, "top10_days": top_days,
                                                           "threshold_max": 0.25, "pass": ds7_pass},
        "C7-3_selection_confirmation_rate_ratio": {"rate_selection": rate_sel, "rate_confirmation": rate_conf,
                                                     "value": c7_3_ratio, "threshold_max": 2.0, "pass": c7_3_pass},
        "ds_gates_summary": ds_all,
        "ds_gates_all_pass": ds_gates_all_pass,
        "confirmation_pool_count_raw": ds1_count,
        "confirmation_event_count_raw": len(event_conf),
        "selection_event_count_raw": len(event_sel),
    }


# ---------------------------------------------------------------------------
# 9-7: PEAD排反性 + E-1〜E-6除外内訳
# ---------------------------------------------------------------------------


def task_9_7(cal, sel_rows, conf_rows, codes, log) -> dict:
    all_rows = sel_rows + conf_rows
    excl_counter_sel: Counter = Counter()
    excl_counter_conf: Counter = Counter()
    for r in sel_rows:
        for e in r["exclusions"]:
            excl_counter_sel[e] += 1
    for r in conf_rows:
        for e in r["exclusions"]:
            excl_counter_conf[e] += 1

    is_cand_sel = sum(1 for r in sel_rows if r["is_candidate"])
    is_cand_conf = sum(1 for r in conf_rows if r["is_candidate"])
    consistency_sel = (is_cand_sel + (len(sel_rows) - is_cand_sel)) == len(sel_rows)
    consistency_conf = (is_cand_conf + (len(conf_rows) - is_cand_conf)) == len(conf_rows)

    e3_excluded_stock_days = [r for r in all_rows if "E3" in r["exclusions"]]

    fins_all = gc.load_fins_summary_all()
    codes_set = set(codes)
    fins_candidates = [r for r in fins_all if r.get("Code") in codes_set]
    disc_dates_by_code = ge.build_disc_dates_by_code(fins_candidates)
    total_disc_date_pairs = sum(len(v) for v in disc_dates_by_code.values())

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

    log.append(
        f"[9-7] excl_sel={dict(excl_counter_sel)} excl_conf={dict(excl_counter_conf)} "
        f"consistency_sel={consistency_sel} consistency_conf={consistency_conf} "
        f"e3_total={len(e3_excluded_stock_days)} intersection_violations={len(intersection_violations)}"
    )

    return {
        "selection_total_code_day_rows": len(sel_rows), "confirmation_total_code_day_rows": len(conf_rows),
        "selection_exclusion_counts": dict(excl_counter_sel), "confirmation_exclusion_counts": dict(excl_counter_conf),
        "selection_is_candidate_count": is_cand_sel, "confirmation_is_candidate_count": is_cand_conf,
        "consistency_check_selection": consistency_sel, "consistency_check_confirmation": consistency_conf,
        "e3_excluded_stock_day_count_total": len(e3_excluded_stock_days),
        "e3_excluded_stock_day_count_selection": sum(1 for r in sel_rows if "E3" in r["exclusions"]),
        "e3_excluded_stock_day_count_confirmation": sum(1 for r in conf_rows if "E3" in r["exclusions"]),
        "disc_date_total_pairs_all_candidate_codes": total_disc_date_pairs,
        "distinct_codes_with_disc_dates": len(disc_dates_by_code),
        "is_candidate_total_rows_checked": len(is_cand_all),
        "intersection_violations_count": len(intersection_violations),
        "intersection_violations_examples": intersection_violations[:20],
        "intersection_is_empty": len(intersection_violations) == 0,
    }


# ---------------------------------------------------------------------------
# 9-8: UL/LL
# ---------------------------------------------------------------------------


def task_9_8(codes: list[str], log: list[str]) -> dict:
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
            if not (v2.CONTRACT_START <= r["Date"] <= v2.CONTRACT_END):
                continue
            total_rows += 1
            for raw, is_ul in ((r.get("UL"), True), (r.get("LL"), False)):
                (ul_counter if is_ul else ll_counter)[repr(raw)] += 1
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
    log.append(f"[9-8] total_rows={total_rows} anomalous={sorted(anomalous_values)} missing_rate={missing_rate} gate={gate_pass}")
    return {
        "total_bars_rows_scanned_pinned_range": total_rows,
        "UL_unique_values_and_counts": dict(ul_counter), "LL_unique_values_and_counts": dict(ll_counter),
        "combined_missing_rate": missing_rate, "anomalous_values": sorted(anomalous_values),
        "values_confined_to_1_0_empty_null": values_confined, "gate_pass": gate_pass,
    }


# ---------------------------------------------------------------------------
# 9-9: ローリング窓による先頭欠けの開示
# ---------------------------------------------------------------------------


def task_9_9(cal: v2.CalendarV2, codes: list[str], log: list[str]) -> dict:
    v2meta = json.loads((v2.PEAD_RESULT_DIR / "candidate_codes_v2.json").read_text(encoding="utf-8"))
    new_codes = set(v2meta["newly_added_vs_old_511_codes"])

    later_start_codes = []
    for c in codes:
        first = cal.raw_first_date.get(c)
        if first is not None and first > v2.CONTRACT_START:
            later_start_codes.append(
                {"code": c, "raw_first_date": first, "raw_last_date": cal.raw_last_date.get(c),
                 "in_newly_added_90": c in new_codes,
                 "likely_cause": "rolling_window_fetched_today" if c in new_codes else "genuine_later_listing_or_other"}
            )
    later_start_codes.sort(key=lambda r: r["raw_first_date"])

    T_window = cal.T[0:60]
    va_loss_detail = []
    for entry in later_start_codes:
        c = entry["code"]
        bd = cal.bars_by_code.get(c, {})
        count_in_window = sum(1 for d in T_window if d in bd and bd[d].get("Va") is not None)
        va_loss_detail.append({"code": c, "va_count_in_U4_window_T1_T60": count_in_window})
    u4_below_30_count = sum(1 for r in va_loss_detail if r["va_count_in_U4_window_T1_T60"] < 30)

    # (d) sigma_gap の有効ギャップが60本に満たずイベント候補から除外された銘柄日の件数
    # （§3.4 C-2 の既存計数。i=68時点でのカバレッジのみ簡易確認: 60本の要件は
    # ge.build_valid_positions/ge.sigma_gapが既にC-2として処理しているため、9-3/9-4/9-6の
    # 実行結果（exclusion_counts の insufficient_window）を参照する設計とし、ここでは
    # 新規銘柄の先頭欠け1日がσ_gap60本要件に与える影響のみを個別に注記する）。

    log.append(
        f"[9-9] later_start_codes={len(later_start_codes)} "
        f"(in_new90={sum(1 for r in later_start_codes if r['in_newly_added_90'])}) "
        f"u4_window_va_below_30={u4_below_30_count}"
    )

    return {
        "T1_pinned": cal.at(1),
        "codes_with_raw_first_date_after_T1_count": len(later_start_codes),
        "codes_with_raw_first_date_after_T1_detail": later_start_codes,
        "fetch_execution_date_inference_note": (
            "raw_first_dateが契約開始日2016-09-15と一致するかを代理指標とし、"
            "candidate_codes_v2.json構築時に記録した新規90銘柄リストとの一致で分類した"
            "（個々のAPI呼び出しタイムスタンプはログに残していない）。"
        ),
        "va_count_in_U4_window_detail": va_loss_detail,
        "U4_valid_va_below_30_count": u4_below_30_count,
        "U4_existing_rule_threshold": 30,
        "sigma_gap_60_requirement_note": (
            "σ_gapの有効ギャップ60本要件（C-2）は先頭欠け1日の影響を受けうるのはi=68"
            "（窓T[2]〜T[67]）の1点のみであり、その銘柄が失う有効ギャップはg(T[2])の1本のみ"
            "（60本の要件は依然満たされる。spec §2.0.1に既に明記済みの分析と一致）。"
            "実際の除外件数は9-6のz*較正・DSゲート計算過程で is_candidate=False の"
            "exclusions=['insufficient_window'] として機械的にカウントされている"
            "（新しい計数を本タスクでは追加していない）。"
        ),
        "note": "新しい閾値は導入していない。既存のU-4/C-2規則で機械的に処理されることの確認のみ",
    }


# ---------------------------------------------------------------------------
# params.json
# ---------------------------------------------------------------------------


def build_params(cal, t_recon, r9_2, r9_3, backup, r9_6, z_star) -> dict:
    return {
        "generated_from": "gap_feasibility_v2.py",
        "spec_reference": "research/EXP-OBS000006/01-spec.md（第2版）",
        "seed": 20260915,
        "raw_data_backup_path": backup["raw_data_backup_path"],
        "raw_data_backup_verified": backup["backup_exists"],
        "t_contract_range_pin": [v2.CONTRACT_START, v2.CONTRACT_END],
        "T_reconstruction": t_recon,
        "price_adjustment_convention": r9_2,
        "dividend_construction": {
            "method": "spec §3.1.1 E-2A(銘柄別・厳密)+E-2B(暦ベース・一括)の和集合",
            "v1_v5_all_pass": r9_3["gate_v1_v5_all_pass"] if r9_3 else None,
        } if r9_3 else None,
        "z_star_frozen": z_star,
        "z_star_grid_27points": GRID,
        "z_star_year_days_constant": YEAR_DAYS,
        "z_star_target_N_ann": TARGET_N_ANN,
        "ds_gates_all_pass": r9_6["ds_gates_all_pass"] if r9_6 else None,
        "ds_gates_summary": r9_6["ds_gates_summary"] if r9_6 else None,
        "signal_definition": {
            "g": "ln(O(j,T[i])/C(j,T[i-1]))",
            "sigma_gap": "有効ギャップちょうど60本（遡り上限66営業日）の標本標準偏差(ddof=1)",
            "z": "g/sigma_gap", "S": "-z",
            "pool_threshold": -1.5, "event_threshold_z_star": z_star,
        },
        "prediction_window": {
            "horizon_primary_H": 5, "entry_price": "O(t1)（イベント日Dの翌営業日始値）",
            "gross_return_formula": "R_5(j) = C(T[i+5]) / O(T[i+1]) - 1",
        },
        "position_sizing": {"position_cap_jpy": 1_250_000, "max_concurrent_positions": 5,
                             "max_same_event_day_positions": 2, "unit_shares": 100},
        "cost_model_summary": {
            "roundtrip_cost_pct_primary": 0.0045, "roundtrip_cost_sensitivity": [0.0035, 0.0045, 0.0060],
            "margin_interest_annual_pct": 0.028,
        },
        "japan_specific_conditions_status": {
            "A_lot_size_100shares": "9タスク段階では未実施（G2到達時に実装）",
            "B_price_limit_ul_ll": "規約確定済み（E-4/E-5/E-7/E-8として候補判定に組込み済み）",
            "C_trading_hours": "記述統計は9-5/9-6段階では未実施（G2到達時）",
            "D_earnings_date_avoidance": "未実装（/fins/earnings-date 未取得。G2到達時に取得予定）",
            "E_margin_requirement": "未実施（G2フェーズ）",
            "F_credit_regulation_unsimulatable": "模擬不能（データ取得不可・C-5・HTTP403）",
            "G_margin_interest_rate": "未実施（G2フェーズ。年率2.8%×実保有暦日数/365）",
            "H_short_leg": "該当なし（ロングオンリー）",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
