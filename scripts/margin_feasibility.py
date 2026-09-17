#!/usr/bin/env python3
"""EXP-OBS000008（信用倍率単独）§9 先行タスク（9-0〜9-10）の実測スクリプト。

spec: `research/EXP-OBS000008/01-spec.md`（第2版）§2・§9。

**このスクリプトは `data/raw/` を一切書き込まない（読み取り専用）。** 既存キャッシュ
（`data/raw/margin_interest/`・`data/raw/pead/bars_daily/`）の上書き・再取得・削除は行わない。

判定語は書かない。すべて実測値・件数のみ。

**第2版での変更点**: §9-9（分割・併合の前方窓混入検出）は単独ではもはやG1をブロックしない
（spec §4.3.1・§4.3参照。G1のR_5計算式をAdjO/AdjCベースへ変更したことで構造的に中和済みと
扱うため）。§9-9bを新設し、Adj系列（AdjO/AdjC/AdjFactor）への信頼が妥当であることの適合性検査
（EXP-OBS000006 §9-3 V-6と同型）を実施する。9-9bで解消できない不整合が見つかった場合のみK-6。

再現用コマンド:
    python3 scripts/margin_feasibility.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gap_common as gc  # noqa: E402
from lib import margin_common as mc  # noqa: E402
from lib import v2_common as v2  # noqa: E402
from lib.pead_common import AnomalousFlagValueError, parse_ul_ll_flag  # noqa: E402

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000008" / "10-result"

# spec §2.0.1（PEAD/ギャップと同一のTを再利用）
EXPECTED_T = {"N": 2441, "T1": "2016-09-15", "TN": "2026-09-14"}
# spec §2.0.2（00-prescreen.md §9.1実測値）
EXPECTED_W = {
    "N_w": 507, "W1": "2016-09-23", "WN": "2026-09-11",
    "m_w": 253, "Wm": "2021-09-17", "Wm_plus_1": "2021-09-24",
}
BUFFER_B = 4  # spec §2.1〜2.2（変更禁止・凍結値）
MIN_EVENTS_PER_WEEK = 5  # spec §6.1-2（G1で使うが、9-3のDS-2/C7-1判定にも同一量を使う）

# 新規に取得した margin_interest 用バックアップ（本セッションで9-0として作成。既存data/rawへの
# 書き込みは一切行わず、純粋な追加コピーとしてのバックアップ）
MARGIN_BACKUP_PATH = "/home/user/backups/data_raw_margin_interest_backup_20260917.tar.gz"


def main() -> int:  # noqa: C901
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    feasibility: dict = {
        "generated_from": "margin_feasibility.py",
        "spec_reference": "research/EXP-OBS000008/01-spec.md §9",
    }
    stop_reason: str | None = None
    escalate_kind: str | None = None

    # ---------------- 9-0: バックアップ確認 ----------------
    pead_backup = v2.verify_backup()
    margin_backup_path = Path(MARGIN_BACKUP_PATH)
    margin_backup = {
        "margin_interest_backup_path": MARGIN_BACKUP_PATH,
        "margin_interest_backup_exists": margin_backup_path.exists(),
        "margin_interest_backup_size_bytes": margin_backup_path.stat().st_size if margin_backup_path.exists() else None,
        "note": (
            "既存の data_raw_backup_20260915.tar.gz（2026-09-15作成）は margin_interest 取得"
            "（0-25・2026-09-16実行）より前に作成されたため margin_interest を含まない"
            "（tar内容の grep で0件を確認済み）。このバックアップの欠落を埋めるため、本セッションで"
            "追加のバックアップ（このパス）を新規作成した。data/raw への書き込みは行っていない"
            "（バックアップ作成はコピー生成のみで、既存ファイルの変更・削除ではない）。"
        ),
    }
    r9_0 = {"pead_bars_daily_backup": pead_backup, "margin_interest_backup": margin_backup}
    feasibility["9-0_backup_verification"] = r9_0
    log.append(
        f"[9-0] pead_backup_exists={pead_backup['backup_exists']} "
        f"margin_backup_exists={margin_backup['margin_interest_backup_exists']}"
    )
    if not (pead_backup["backup_exists"] and margin_backup["margin_interest_backup_exists"]):
        stop_reason = "9-0: バックアップが確認できない"
        escalate_kind = "K-6"

    # ---------------- 9-1: 候補集合の再構成 ----------------
    r9_1 = None
    codes: list[str] = []
    if stop_reason is None:
        r9_1, codes = task_9_1(log)
        feasibility["9-1_candidate_reconstruction"] = r9_1
        if not r9_1["reconstruction_valid"]:
            stop_reason = "9-1: 候補集合の再構成に失敗（取得不能または既存v2との不整合）"
            escalate_kind = "K-6"

    # ---------------- T・W の再構成・照合（9-2） ----------------
    cal = None
    all_series = None
    W: list[str] = []
    r9_2 = None
    if stop_reason is None:
        cal = v2.CalendarV2(codes)
        all_series = mc.build_all_series(codes)
        W = mc.build_weekly_calendar(all_series)
        r9_2 = task_9_2(cal, W, log)
        feasibility["9-2_T_W_reconstruction"] = r9_2
        if not (r9_2["T_matches_expected"] and r9_2["W_matches_expected"]):
            stop_reason = "9-2: T または W の再構成結果がspec §2.0.1/§2.0.2の期待値と一致しない"
            escalate_kind = "K-6"

    # ---------------- 9-3: DS-1〜DS-6 再評価 ----------------
    r9_3 = None
    sel_range_w = conf_range_w = None
    by_date_index = None
    if stop_reason is None:
        by_date_index = mc.index_delta_m_by_date(all_series)
        N_w = len(W)
        m_w = N_w // 2
        sel_range_w = (1, m_w - BUFFER_B)
        conf_range_w = (m_w + 1, N_w - BUFFER_B)
        r9_3 = task_9_3(codes, W, by_date_index, m_w, sel_range_w, conf_range_w, log)
        feasibility["9-3_data_sufficiency_gate"] = r9_3
        if not r9_3["all_pass"]:
            stop_reason = (
                "9-3: データ十分性ゲート(DS-1〜DS-6)のいずれかが未達。K-5に従いSへ差し戻す"
                "（判定不能。確認期間のリターンは未見のまま温存）。"
            )
            escalate_kind = "K-5"

    # ---------------- 9-4: Mrgn/MrgnNm ショート建て可否の対応関係 ----------------
    if stop_reason is None:
        r9_4 = task_9_4(log)
        feasibility["9-4_short_eligibility_field_mapping"] = r9_4

    # ---------------- 9-5: 全507週 W[w]∈T の全数検証 ----------------
    if stop_reason is None:
        r9_5 = task_9_5(cal, W, log)
        feasibility["9-5_all_weeks_in_T"] = r9_5
        if r9_5["weeks_not_in_T_count"] > 0:
            stop_reason = "9-5: W[w]がTに含まれない週が存在する"
            escalate_kind = "K-6"

    # ---------------- 9-6: バッファb=4の重なり排除の全数検証 ----------------
    if stop_reason is None:
        r9_6 = task_9_6(cal, W, sel_range_w, conf_range_w, log)
        feasibility["9-6_buffer_overlap_check"] = r9_6
        if r9_6["overlap_violation_count"] > 0:
            stop_reason = "9-6: バッファb=4では前方窓の重なりを排除できない週が存在する"
            escalate_kind = "K-6"

    # ---------------- 9-7: ΔM_w(j)の分布 ----------------
    if stop_reason is None:
        r9_7 = task_9_7(all_series, sel_range_w, conf_range_w, W, log)
        feasibility["9-7_delta_m_distribution"] = r9_7

    # ---------------- 9-8: 決算跨ぎ制約エンドポイント確認 ----------------
    if stop_reason is None:
        r9_8 = task_9_8(codes, log)
        feasibility["9-8_earnings_date_endpoint_check"] = r9_8

    # ---------------- 9-9: 分割・併合による前方窓汚染検査（第2版: 検出自体はG1をブロックしない） ----------------
    r9_9 = None
    if stop_reason is None:
        r9_9 = task_9_9(cal, W, by_date_index, sel_range_w, conf_range_w, log)
        feasibility["9-9_corporate_action_in_forward_window"] = r9_9
        # spec第2版 §9-9・§6.8注記: 第2版ではこの検出自体はK-6の理由にならない
        # （§4.3.1のAdj系列置換により構造的に中和済みと扱う）。件数の記録・監査証跡としての
        # 出力は引き続き必須（上記で実施済み）。K-6に該当するのは9-9bで解消できない不整合の
        # みであり、9-9の件数それ自体では stop_reason を設定しない。

    # ---------------- 9-9b（第2版で新設）: Adj系列適合性検査（EXP-OBS000006 §9-3 V-6と同型） ----------------
    if stop_reason is None:
        r9_9b = task_9_9b(cal, codes, r9_9, log)
        feasibility["9-9b_adj_price_conformance"] = r9_9b
        if not r9_9b["gate_pass"]:
            # spec §9-9bは「(a)(b)いずれかで違反が見つかった場合、該当(code,week)ペアのみ
            # 機械的に除外して続行してよい」と規定するが、本実測ではcheck(b)の違反274件を
            # 文字通り適用すると9-9の該当1305件中1050件（80.4%）が除外対象となり、
            # §4.3.1が明記する不変条件（除外は行わずサンプル数は不変）と規模の面で正面から
            # 矛盾する。違反はいずれも「その銘柄にとってTピン留め範囲内で最後の分割・併合"
            # イベント当日はAdjO=O・AdjC=Cになる」という単一の機械的パターンに100%一致しており、
            # これがAdjFactor/AdjOの正式仕様なのかspec check(b)の想定違いなのかをB実装チームの
            # 権限では判定できない（推測で埋めない・T-8/EXP-OBS000037と同型のため自己解釈で
            # 埋めずSに差し戻す）。
            stop_reason = (
                "9-9b: check(b)の違反274件（AdjFactor≠1だがAdjO=O/AdjC=C）が、機械的検算により"
                "すべて『Tピン留め範囲内でその銘柄にとって最後の分割・併合イベント』という単一パターンに"
                "一致することが判明。spec §9-9bの部分的フォールバックを文字通り適用すると9-9該当"
                "1305件中1050件（80.4%）を除外することになり、§4.3.1の不変条件（サンプル数は不変）と"
                "規模の面で矛盾するため、B実装チームの権限で機械的に適用せず停止した。"
            )
            escalate_kind = "K-6"

    # ---------------- 9-10: UL/LLの全ユニーク値・欠損率 ----------------
    if stop_reason is None:
        r9_10 = task_9_10(codes, cal, log)
        feasibility["9-10_ul_ll_flag_check"] = r9_10
        if not r9_10["gate_pass"]:
            stop_reason = "9-10: UL/LLの値がパース規則を満たさない、または欠損率が1%を超える"
            escalate_kind = "K-6"

    feasibility["stop_reason"] = stop_reason
    feasibility["escalate_to_S"] = stop_reason is not None
    feasibility["escalate_kind"] = escalate_kind
    feasibility["can_proceed_to_G1"] = stop_reason is None

    params = build_params(cal, W, r9_1, r9_2 if stop_reason is None else None, r9_0, feasibility)

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
# 9-1: 候補集合の再構成
# ---------------------------------------------------------------------------


def task_9_1(log: list[str]) -> tuple[dict, list[str]]:
    """spec 9-1: D_sel/D_conf時点のU-1/U-2通過銘柄の和集合として候補集合を再構成する。

    本タスク起動時の指示（オーケストレータからのタスク説明）により、PEAD/ギャップが
    既に同一手続き（D_SEL_V2=2016-12-15・D_CONF_V2=2021-09-16時点のU-1/U-2和集合。
    `candidate_codes_v2.json`）で構築済みであり、かつ `data/raw/margin_interest/` は
    この554銘柄集合に対して取得されたものであるため、**同じ確定日を再利用**し、
    新規のmaster再取得は行わずv2の成果物をそのまま候補集合v3として採用する。
    """
    codes_v2 = v2.load_candidate_codes_v2()
    v2meta = json.loads((v2.PEAD_RESULT_DIR / "candidate_codes_v2.json").read_text(encoding="utf-8"))

    margin_files = sorted(p.stem for p in mc.MARGIN_RAW_DIR.glob("*.json") if p.stem.isdigit())
    margin_codes_set = set(margin_files)
    codes_v2_set = set(codes_v2)
    exact_match = margin_codes_set == codes_v2_set

    out = {
        "codes": sorted(codes_v2_set),
        "count": len(codes_v2_set),
        "reused_determination_dates": {
            "D_sel": v2.D_SEL_V2.isoformat(),
            "D_conf": v2.D_CONF_V2.isoformat(),
            "source": "research/EXP-OBS000005/10-result/candidate_codes_v2.json",
            "reuse_rationale": (
                "data/raw/margin_interest/ は0-25実行時にcandidate_codes_v2.json（554銘柄）を"
                "codes-fileとして指定して取得されたものであり（margin_fetch_data.pyのDEFAULT_CODES_FILE"
                "参照）、margin_interestデータの母集合とPEAD/ギャップのv2候補集合は定義上同一である。"
                "したがって同一のD_sel/D_conf確定日（2016-12-15/2021-09-16）におけるU-1・U-2通過銘柄の"
                "和集合として再構成する作業は、EXP-OBS000005のpead_feasibility_v2.pyで既に実行済みであり、"
                "本タスクではmaster再取得を行わず既存成果物を再利用した（オーケストレータのタスク指示で"
                "明示的に許可された経路）。"
            ),
            "pead_v2_metadata": v2meta,
        },
        "margin_interest_raw_files_count": len(margin_files),
        "margin_interest_codes_equal_candidate_v2_codes": exact_match,
        "margin_interest_codes_not_in_v2": sorted(margin_codes_set - codes_v2_set),
        "v2_codes_not_in_margin_interest": sorted(codes_v2_set - margin_codes_set),
    }
    out["reconstruction_valid"] = exact_match
    log.append(
        f"[9-1] candidate_count={out['count']} margin_files={len(margin_files)} "
        f"exact_match={exact_match}"
    )
    (RESULT_DIR / "candidate_codes_v3.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out, sorted(codes_v2_set)


# ---------------------------------------------------------------------------
# 9-2: T・W の再構成・照合
# ---------------------------------------------------------------------------


def task_9_2(cal: v2.CalendarV2, W: list[str], log: list[str]) -> dict:
    N = len(cal.T)
    N_w = len(W)
    m_w = N_w // 2
    t_check = {"N": N, "T1": cal.at(1), "TN": cal.at(N)}
    t_matches = (
        t_check["N"] == EXPECTED_T["N"] and t_check["T1"] == EXPECTED_T["T1"] and t_check["TN"] == EXPECTED_T["TN"]
    )
    w_check = {
        "N_w": N_w, "W1": W[0] if W else None, "WN": W[-1] if W else None,
        "m_w": m_w, "Wm": W[m_w - 1] if m_w >= 1 else None,
        "Wm_plus_1": W[m_w] if m_w < N_w else None,
    }
    w_matches = (
        w_check["N_w"] == EXPECTED_W["N_w"] and w_check["W1"] == EXPECTED_W["W1"]
        and w_check["WN"] == EXPECTED_W["WN"] and w_check["m_w"] == EXPECTED_W["m_w"]
        and w_check["Wm"] == EXPECTED_W["Wm"] and w_check["Wm_plus_1"] == EXPECTED_W["Wm_plus_1"]
    )
    log.append(f"[9-2] T={t_check} matches={t_matches} | W={w_check} matches={w_matches}")
    return {
        "T_checkpoints": t_check, "T_expected": EXPECTED_T, "T_matches_expected": t_matches,
        "W_checkpoints": w_check, "W_expected": EXPECTED_W, "W_matches_expected": w_matches,
        "missing_bars_codes": cal.missing_bars_codes,
    }


# ---------------------------------------------------------------------------
# 9-3: DS-1〜DS-6
# ---------------------------------------------------------------------------


def task_9_3(codes, W, by_date_index, m_w, sel_range_w, conf_range_w, log) -> dict:
    N_w = len(W)

    def stock_weeks_in_range(lo_w: int, hi_w: int):
        """1-indexed週範囲[lo_w, hi_w]内の(code, delta_m_valid)ペア列を返す。"""
        rows = []
        for w in range(lo_w, hi_w + 1):
            date = W[w - 1]
            entries = by_date_index.get(date, {})
            for code, entry in entries.items():
                rows.append((w, date, code, entry))
        return rows

    sel_rows = stock_weeks_in_range(*sel_range_w)
    conf_rows = stock_weeks_in_range(*conf_range_w)

    conf_valid_rows = [r for r in conf_rows if r[3]["delta_m_valid"]]
    sel_valid_rows = [r for r in sel_rows if r[3]["delta_m_valid"]]

    # DS-1: 確認期間の有効「銘柄週」観測数
    ds1_count = len(conf_valid_rows)
    ds1_pass = ds1_count >= 300

    # DS-2/C7-1: 確認期間の「5銘柄以上の有効ΔMを持つ週」数K
    conf_valid_by_week = Counter(r[0] for r in conf_valid_rows)
    ds2_weeks = sum(1 for c in conf_valid_by_week.values() if c >= MIN_EVENTS_PER_WEEK)
    ds2_pass = ds2_weeks >= 30

    # DS-3: 確認期間に1回以上出現する候補銘柄数（有効ΔM基準）
    ds3_codes = len(set(r[2] for r in conf_valid_rows))
    ds3_pass = ds3_codes >= 150

    # DS-4: 候補ユニバース×全期間の日足欠損営業日率（EXP-OBS000005実測を援用。候補集合が同一のため）
    pead_feas = json.loads((v2.PEAD_RESULT_DIR / "feasibility.json").read_text(encoding="utf-8"))
    ds4_rate = pead_feas["9-3_missing_rate"]["overall_missing_rate"]
    ds4_pass = ds4_rate is not None and ds4_rate <= 0.05

    # DS-5: 確認期間のShrtVol=0除外率（E-1/E-2に該当する行の比率。分母=確認期間の全「銘柄週」レコード数）
    ds5_excluded = sum(
        1 for r in conf_rows
        if not r[3]["delta_m_valid"]
        and any(x.startswith("E-1") or x.startswith("E-2") for x in r[3]["exclusion_reasons"])
    )
    ds5_denom = len(conf_rows)
    ds5_rate = ds5_excluded / ds5_denom if ds5_denom else None
    ds5_pass = ds5_rate is not None and ds5_rate <= 0.10

    # DS-6: 週次グリッドの異常ギャップ率（E-3該当率。全期間・全候補銘柄・gap_days定義済みの全ペア）
    all_pairs, anomalous_pairs = _scan_all_gap_pairs(codes)
    ds6_rate = anomalous_pairs / all_pairs if all_pairs else None
    ds6_pass = ds6_rate is not None and ds6_rate <= 0.01

    all_pass = ds1_pass and ds2_pass and ds3_pass and ds4_pass and ds5_pass and ds6_pass

    log.append(
        f"[9-3] DS-1={ds1_count}:{ds1_pass} DS-2={ds2_weeks}:{ds2_pass} DS-3={ds3_codes}:{ds3_pass} "
        f"DS-4={ds4_rate}:{ds4_pass} DS-5={ds5_rate}:{ds5_pass} DS-6={ds6_rate}:{ds6_pass} ALL={all_pass}"
    )

    return {
        "selection_week_range_1indexed": list(sel_range_w),
        "confirmation_week_range_1indexed": list(conf_range_w),
        "DS-1_confirmation_valid_stock_week_count": {"value": ds1_count, "threshold_min": 300, "pass": ds1_pass},
        "DS-2_confirmation_weeks_with_ge5_valid_deltaM": {"value": ds2_weeks, "threshold_min": 30, "pass": ds2_pass},
        "DS-3_confirmation_distinct_codes_with_valid_deltaM": {"value": ds3_codes, "threshold_min": 150, "pass": ds3_pass},
        "DS-4_overall_missing_rate_daily_bars": {
            "value": ds4_rate, "threshold_max": 0.05, "pass": ds4_pass,
            "source": "EXP-OBS000005/10-result/feasibility.json の 9-3_missing_rate を援用（候補集合554が同一のため）",
        },
        "DS-5_confirmation_shrtvol_zero_exclusion_rate": {
            "value": ds5_rate, "numerator": ds5_excluded, "denominator": ds5_denom,
            "threshold_max": 0.10, "pass": ds5_pass,
        },
        "DS-6_anomalous_gap_rate": {
            "value": ds6_rate, "numerator": anomalous_pairs, "denominator": all_pairs,
            "threshold_max": 0.01, "pass": ds6_pass,
        },
        "K_equals_DS2": ds2_weeks,
        "all_pass": all_pass,
    }


def _scan_all_gap_pairs(codes: list[str]) -> tuple[int, int]:
    total = 0
    anomalous = 0
    for code in codes:
        raw = mc.load_margin_raw(code)
        if raw is None:
            continue
        canonical, _, _ = mc.build_code_canonical_series(raw)
        series = mc.compute_delta_m_series(canonical)
        for entry in series[1:]:
            total += 1
            if entry["gap_days"] is not None and entry["gap_days"] > mc.GAP_DAYS_MAX:
                anomalous += 1
    return total, anomalous


# ---------------------------------------------------------------------------
# 9-4: Mrgn/MrgnNm ショート建て可否
# ---------------------------------------------------------------------------


def task_9_4(log: list[str]) -> dict:
    pead_params = json.loads((v2.PEAD_RESULT_DIR / "params.json").read_text(encoding="utf-8"))
    mapping_9_2 = pead_params.get("field_value_mapping_9_2", {})
    master_fields = mapping_9_2.get("master_field_unique_values_by_determination_date", {})
    mrgn_nm = master_fields.get("MrgnNm", {})

    result = {
        "source": "研究上の実測（EXP-OBS000005 9-2で確認済みのMrgn/MrgnNm値集合を参照）",
        "MrgnNm_unique_values_d_sel": mrgn_nm.get("d_sel_2016-12-15"),
        "MrgnNm_unique_values_d_conf": mrgn_nm.get("d_conf_2021-09-16"),
        "value_set_identical_both_dates": mrgn_nm.get("value_set_identical"),
        "mapping_used": {
            "Mrgn=1 (MrgnNm='信用')": "制度信用の買建てのみ可能。新規空売りに必要な貸株の仕組みを持たない区分。ショート対象外",
            "Mrgn=2 (MrgnNm='貸借')": "証券金融会社からの貸株を通じて制度信用の空売り（新規売り）が可能な銘柄区分。ショート対象",
            "Mrgn=3 (MrgnNm='その他')": "信用取引不可。ロング・ショートともに対象外",
        },
        "short_eligible_mrgn_values": sorted(mc.MRGN_SHORT_ELIGIBLE_OK),
        "mapping_unambiguous": True,
        "note": (
            "spec §5.2注記が要求する保守的近似（貸借銘柄のみをショート対象とする）を、"
            "MrgnNm='貸借'(Mrgn=2)という一意に定まる標準区分名で直接特定できたため、"
            "推測ではなく確定的な対応付けとして採用した。"
        ),
    }
    log.append(f"[9-4] short_eligible_mrgn_values={result['short_eligible_mrgn_values']} unambiguous=True")
    return result


# ---------------------------------------------------------------------------
# 9-5: 全507週 W[w]∈T
# ---------------------------------------------------------------------------


def task_9_5(cal: v2.CalendarV2, W: list[str], log: list[str]) -> dict:
    not_in_t = []
    for i, d in enumerate(W, start=1):
        if cal.idx(d) is None:
            not_in_t.append({"w": i, "date": d})
    log.append(f"[9-5] total_weeks={len(W)} weeks_not_in_T={len(not_in_t)}")
    return {
        "total_weeks_checked": len(W),
        "weeks_not_in_T_count": len(not_in_t),
        "weeks_not_in_T_detail": not_in_t,
    }


# ---------------------------------------------------------------------------
# 9-6: バッファb=4の重なり排除の全数検証
# ---------------------------------------------------------------------------


def task_9_6(cal: v2.CalendarV2, W: list[str], sel_range_w, conf_range_w, log) -> dict:
    """spec §2.2 W-6: 週wの前方窓はT添字[k+3, k+19]を要する。

    選定期間末尾の週の前方窓が確認期間側へ食い込まないこと・確認期間末尾の週の前方窓が
    Tの終端を超えないことを、実添字で全数検証する。
    """
    N = len(cal.T)
    sel_lo, sel_hi = sel_range_w
    conf_lo, conf_hi = conf_range_w

    def k_of(w: int) -> int | None:
        return cal.idx(W[w - 1])

    # 選定期間末尾週の前方窓上限が確認期間開始週の基準日インデックス未満であることを確認
    sel_last_k = k_of(sel_hi)
    conf_first_k = k_of(conf_lo)
    sel_last_forward_end = sel_last_k + 19 if sel_last_k is not None else None
    overlap_sel_into_conf = (
        sel_last_forward_end is not None and conf_first_k is not None
        and sel_last_forward_end >= conf_first_k
    )

    # 確認期間の全週について k+19 <= N を満たすか全数検証
    violations = []
    for w in range(conf_lo, conf_hi + 1):
        k = k_of(w)
        if k is None:
            violations.append({"w": w, "reason": "k_undefined"})
            continue
        if k + 19 > N:
            violations.append({"w": w, "k": k, "k_plus_19": k + 19, "N": N, "reason": "forward_window_exceeds_T"})

    # 選定期間の全週についても同様に確認（selの前方窓が確認期間の週の基準日を追い越さないこと）
    sel_violations = []
    for w in range(sel_lo, sel_hi + 1):
        k = k_of(w)
        if k is None:
            sel_violations.append({"w": w, "reason": "k_undefined"})
            continue
        if conf_first_k is not None and k + 19 >= conf_first_k:
            sel_violations.append(
                {"w": w, "k": k, "k_plus_19": k + 19, "conf_first_k": conf_first_k, "reason": "forward_window_overlaps_confirmation"}
            )

    total_violations = len(violations) + len(sel_violations) + (1 if overlap_sel_into_conf else 0)
    log.append(
        f"[9-6] buffer_b={BUFFER_B} sel_last_forward_end={sel_last_forward_end} conf_first_k={conf_first_k} "
        f"overlap_sel_into_conf={overlap_sel_into_conf} conf_violations={len(violations)} "
        f"sel_violations={len(sel_violations)}"
    )
    return {
        "buffer_b": BUFFER_B,
        "selection_range_w": list(sel_range_w), "confirmation_range_w": list(conf_range_w),
        "selection_last_week_k": sel_last_k, "selection_last_week_forward_window_end": sel_last_forward_end,
        "confirmation_first_week_k": conf_first_k,
        "overlap_selection_forward_window_into_confirmation": overlap_sel_into_conf,
        "confirmation_forward_window_exceeds_T_violations": violations,
        "selection_forward_window_overlaps_confirmation_violations": sel_violations,
        "overlap_violation_count": total_violations,
    }


# ---------------------------------------------------------------------------
# 9-7: ΔM_w(j)の分布
# ---------------------------------------------------------------------------


def task_9_7(all_series, sel_range_w, conf_range_w, W, log) -> dict:
    sel_dates = set(W[sel_range_w[0] - 1: sel_range_w[1]])
    conf_dates = set(W[conf_range_w[0] - 1: conf_range_w[1]])

    def collect(dates_set):
        vals = []
        for info in all_series.values():
            if info.get("missing"):
                continue
            for entry in info["series"]:
                if entry["delta_m_valid"] and entry["date"] in dates_set:
                    vals.append(entry["delta_m"])
        return vals

    def stats(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0}
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        mean_ = sum(vals_sorted) / n
        var_ = sum((x - mean_) ** 2 for x in vals_sorted) / (n - 1) if n > 1 else None
        std_ = math.sqrt(var_) if var_ is not None else None

        def pct(p):
            idx = min(n - 1, max(0, int(round(p * (n - 1)))))
            return vals_sorted[idx]

        skew = None
        kurt = None
        if std_ and std_ > 0 and n > 2:
            m3 = sum((x - mean_) ** 3 for x in vals_sorted) / n
            m4 = sum((x - mean_) ** 4 for x in vals_sorted) / n
            skew = m3 / (std_ ** 3)
            kurt = m4 / (std_ ** 4) - 3.0
        return {
            "n": n, "mean": mean_, "std": std_, "min": vals_sorted[0], "max": vals_sorted[-1],
            "p1": pct(0.01), "p5": pct(0.05), "p25": pct(0.25), "median": pct(0.5),
            "p75": pct(0.75), "p95": pct(0.95), "p99": pct(0.99),
            "skewness": skew, "excess_kurtosis": kurt,
        }

    sel_vals = collect(sel_dates)
    conf_vals = collect(conf_dates)
    r = {"selection": stats(sel_vals), "confirmation": stats(conf_vals)}
    log.append(f"[9-7] selection_n={r['selection'].get('n')} confirmation_n={r['confirmation'].get('n')}")
    return r


# ---------------------------------------------------------------------------
# 9-8: 決算跨ぎ制約エンドポイント確認
# ---------------------------------------------------------------------------


def task_9_8(codes: list[str], log: list[str]) -> dict:
    earnings_dir = Path(__file__).resolve().parent.parent / "data" / "raw" / "gap" / "earnings_date"
    present = []
    missing = []
    for code in codes:
        if (earnings_dir / f"{code}.json").exists():
            present.append(code)
        else:
            missing.append(code)
    r = {
        "endpoint_used": "/fins/earnings-date",
        "endpoint_forbidden": "/equities/earnings-calendar（使用禁止・N-8）",
        "cache_dir": str(earnings_dir),
        "candidate_codes_count": len(codes),
        "codes_with_cache_count": len(present),
        "codes_missing_cache_count": len(missing),
        "codes_missing_cache": missing,
        "note": (
            "本タスクはG2（§7 D項・決算跨ぎ制約）で使用する前提のエンドポイント確認のみ。"
            "G1（本specの主測定）は決算跨ぎ制約を使用しない。既存キャッシュ（EXP-OBS000006/gap用に"
            "取得済み・511銘柄）は候補集合554のうち一部（missing一覧）を欠くため、G2に進む場合は"
            "不足分の追加取得（新規銘柄のみ・既存キャッシュの再取得ではない）が必要になる可能性がある。"
        ),
    }
    log.append(f"[9-8] present={len(present)} missing={len(missing)}")
    return r


# ---------------------------------------------------------------------------
# 9-9: 分割・併合による前方窓汚染検査
# ---------------------------------------------------------------------------


def task_9_9(cal: v2.CalendarV2, W: list[str], by_date_index, sel_range_w, conf_range_w, log) -> dict:
    def check(lo_w: int, hi_w: int) -> tuple[int, list[dict]]:
        checked = 0
        violations = []
        for w in range(lo_w, hi_w + 1):
            date = W[w - 1]
            entries = by_date_index.get(date, {})
            k = cal.idx(date)
            if k is None:
                continue
            for code, entry in entries.items():
                if not entry["delta_m_valid"]:
                    continue
                checked += 1
                for kk in range(k + 1, k + 20):  # t1..t19 (W-6: k+3..k+19の範囲を含む保守的な広め窓)
                    date_kk = cal.at(kk)
                    if date_kk is None:
                        continue
                    row = cal.row(code, date_kk)
                    if row is None:
                        continue
                    af = row.get("AdjFactor")
                    if af is not None and abs(af - 1.0) > 1e-9:
                        violations.append({"code": code, "week_date": date, "window_date": date_kk, "AdjFactor": af})
        return checked, violations

    sel_checked, sel_viol = check(*sel_range_w)
    conf_checked, conf_viol = check(*conf_range_w)
    total = len(sel_viol) + len(conf_viol)
    log.append(
        f"[9-9] selection_checked={sel_checked} selection_violations={len(sel_viol)} "
        f"confirmation_checked={conf_checked} confirmation_violations={len(conf_viol)} total={total}"
    )
    return {
        "methodology": "有効ΔM_wを持つ(code,週)ペアの[k+1,k+19]窓にAdjFactor≠1の行を含む件数",
        "selection_checked": sel_checked, "selection_violations": sel_viol,
        "confirmation_checked": conf_checked, "confirmation_violations": conf_viol,
        "total_violations": total,
    }


# ---------------------------------------------------------------------------
# 9-9b（第2版で新設）: Adj系列適合性検査（EXP-OBS000006 §9-3 V-6と同型）
# ---------------------------------------------------------------------------


def task_9_9b(cal: v2.CalendarV2, codes: list[str], r9_9: dict, log: list[str]) -> dict:
    """spec §4.3.1・§9タスク表 9-9b:

    (a) `AdjFactor≠1`の全行が`is_split_merger_row(row)`で捕捉されること
    (b) `AdjFactor≠1`の全行で`AdjO`/`AdjC`が非null・生値`O`/`C`と異なる値を持つこと
        （候補554銘柄の`bars_daily`全行をスキャン。EXP-OBS000006 V-6と同一の走査範囲）
    (c) §9-9で検出した624+681件の`(code,week)`ペアそれぞれについて、生値ベースの`R_5`と
        `Adj`系列ベースの`R_5`を両方算出し差分を記録する（判定には使わない。監査証跡）

    (a)(b)いずれかで違反が見つかった場合、その違反行の日付を含む前方窓を持つ
    `(code,week)`ペア（9-9の該当リストのサブセット）を機械的に特定し、
    `fallback_excluded_pairs`として出力する（G1側で個別除外するための入力）。
    """
    # ---- (a)(b): 候補554銘柄のbars_daily全行を走査 ----
    total_rows = 0
    adjfactor_nonunity_counter: Counter = Counter()
    a_violations: list[dict] = []
    b_violations: list[dict] = []

    for code in codes:
        bd = cal.bars_by_code.get(code)
        if not bd:
            continue
        for date_str, r in bd.items():
            total_rows += 1
            af = r.get("AdjFactor")
            af_nonunity = af is not None and abs(af - 1.0) > 1e-9
            if not af_nonunity:
                continue
            adjfactor_nonunity_counter[af] += 1

            captured = gc.is_split_merger_row(r)
            if not captured:
                a_violations.append({"code": code, "date": date_str, "AdjFactor": af})

            adj_o, adj_c = r.get("AdjO"), r.get("AdjC")
            o, c = r.get("O"), r.get("C")
            b_ok = (
                adj_o is not None and adj_c is not None
                and adj_o != o and adj_c != c
            )
            if not b_ok:
                b_violations.append(
                    {"code": code, "date": date_str, "AdjFactor": af, "O": o, "AdjO": adj_o, "C": c, "AdjC": adj_c}
                )

    a_pass = len(a_violations) == 0
    b_pass = len(b_violations) == 0

    # ---- (b)違反の機械的な原因切り分け（判定はしない。事実の記録のみ） ----
    # 各b違反行について、同一銘柄のbars_daily全行（Tのピン留め範囲内）に、その日付より
    # 後の日付でAdjFactor≠1の行が存在するか（＝その銘柄にとって「直近最後の分割・併合」
    # ではないか）を機械的に確認する。
    b_violations_with_later_split = []
    for v in b_violations:
        code = v["code"]
        bd = cal.bars_by_code.get(code, {})
        later_nonunity_dates = sorted(
            d for d, r in bd.items()
            if d > v["date"] and r.get("AdjFactor") is not None and abs(r["AdjFactor"] - 1.0) > 1e-9
        )
        b_violations_with_later_split.append(
            {**v, "has_later_split_in_pinned_range": bool(later_nonunity_dates), "later_split_dates": later_nonunity_dates}
        )
    b_violations_all_are_last_split_in_series = (
        len(b_violations) > 0 and all(not e["has_later_split_in_pinned_range"] for e in b_violations_with_later_split)
    )

    # ---- 違反行 -> 該当(code,week)ペアの機械的特定（(a)(b)いずれかの違反があった場合のみ意味を持つ） ----
    violating_code_dates = {(v["code"], v["date"]) for v in a_violations} | {(v["code"], v["date"]) for v in b_violations}
    fallback_excluded_pairs: list[dict] = []
    if violating_code_dates:
        for label, viol_list in (("selection", r9_9["selection_violations"] if r9_9 else []),
                                  ("confirmation", r9_9["confirmation_violations"] if r9_9 else [])):
            for v in viol_list:
                code = v["code"]
                if (code, v["window_date"]) in violating_code_dates:
                    fallback_excluded_pairs.append({"period": label, "code": code, "week_date": v["week_date"], "window_date": v["window_date"]})

    # 特定できなければ全体設計をやり直す必要がある構造異常（想定外）。違反があるのに
    # 1件もマッピングできなければフォールバックが機能しないためgate_pass=Falseとする。
    mapping_ok = (len(violating_code_dates) == 0) or (len(fallback_excluded_pairs) > 0)

    # ---- (c): 9-9の624+681件それぞれについて生値R_5とAdj系列R_5を算出・差分記録（監査証跡のみ） ----
    diff_records: list[dict] = []
    if r9_9 is not None:
        for label, viol_list in (("selection", r9_9["selection_violations"]), ("confirmation", r9_9["confirmation_violations"])):
            for v in viol_list:
                code = v["code"]
                week_date = v["week_date"]
                k = cal.idx(week_date)
                rec = {"period": label, "code": code, "week_date": week_date}
                if k is None:
                    rec.update(t1=None, t5=None, raw_R5=None, adj_R5=None, diff=None, note="k_undefined")
                    diff_records.append(rec)
                    continue
                t1, t5 = cal.at(k + 4), cal.at(k + 9)
                row1 = cal.row(code, t1) if t1 else None
                row5 = cal.row(code, t5) if t5 else None
                o1 = row1.get("O") if row1 else None
                c5 = row5.get("C") if row5 else None
                adjo1 = row1.get("AdjO") if row1 else None
                adjc5 = row5.get("AdjC") if row5 else None
                raw_r5 = (float(c5) / float(o1) - 1.0) if (o1 is not None and c5 is not None and float(o1) > 0) else None
                adj_r5 = (float(adjc5) / float(adjo1) - 1.0) if (adjo1 is not None and adjc5 is not None and float(adjo1) > 0) else None
                diff = (adj_r5 - raw_r5) if (raw_r5 is not None and adj_r5 is not None) else None
                rec.update(t1=t1, t5=t5, raw_R5=raw_r5, adj_R5=adj_r5, diff=diff)
                diff_records.append(rec)

    # gate_pass: (a)は0件必須（構造的に自明だが検算）。(b)は文字通りには274件の違反があり、
    # 全674...ではなく全274件が「その銘柄にとって範囲内で最後（最新）の分割・併合イベント」
    # という単一の機械的パターンに一致する（後段でチェック済み・例外0件）。この場合の
    # fallback_excluded_pairs（1305件中の大半）をそのまま適用すると§4.3.1が明記する不変条件
    # 「サンプル数は不変（選定134,963件・確認130,235件）」と直接矛盾する規模になるため、
    # spec §9-9bの部分的フォールバック手続きを自己判断で機械的に適用せず、ここで停止しSに
    # 差し戻す（T-8/EXP-OBS000037と同型の「窓・フィールド意味論の矛盾を自己解釈で埋めない」
    # 原則に基づく）。
    escalation_required = not (a_pass and b_pass)
    gate_pass = a_pass and b_pass

    log.append(
        f"[9-9b] total_rows_scanned={total_rows} a_violations={len(a_violations)} "
        f"b_violations={len(b_violations)} b_violations_all_last_split_in_series={b_violations_all_are_last_split_in_series} "
        f"fallback_excluded_pairs_if_applied={len(fallback_excluded_pairs)}/{(len(r9_9['selection_violations']) + len(r9_9['confirmation_violations'])) if r9_9 else 0} "
        f"diff_records={len(diff_records)} gate_pass={gate_pass}"
    )

    return {
        "methodology": (
            "EXP-OBS000006 §9-3 V-6と同型。候補554銘柄のbars_daily全行を走査し、"
            "(a) AdjFactor≠1の全行がis_split_merger_row()で捕捉されるか、"
            "(b) AdjFactor≠1の全行でAdjO/AdjCが非null・生値と異なるか、を検査する。"
            "(c) §9-9で検出した(code,week)ペアそれぞれについて生値R_5とAdj系列R_5の差分を"
            "監査証跡として記録する（判定には使わない）。"
        ),
        "total_bars_rows_scanned": total_rows,
        "AdjFactor_non_unity_row_count": sum(adjfactor_nonunity_counter.values()),
        "AdjFactor_non_unity_unique_values_and_counts": {str(k): v for k, v in adjfactor_nonunity_counter.items()},
        "check_a_all_nonunity_captured_by_is_split_merger_row": {
            "violations_count": len(a_violations), "violations": a_violations, "pass": a_pass,
        },
        "check_b_adjO_adjC_nonnull_and_differs_from_raw": {
            "violations_count": len(b_violations), "violations": b_violations_with_later_split, "pass": b_pass,
            "diagnostic_all_violations_are_codes_last_split_in_pinned_range": b_violations_all_are_last_split_in_series,
            "diagnostic_note": (
                "検算（判定ではない）: 274件のb違反すべてについて、同一銘柄のTピン留め範囲内で"
                "その日付より後にAdjFactor≠1の行が存在しない（＝範囲内で最後の分割・併合イベント）"
                "という単一パターンに100%一致することを機械的に確認した。spec §4.3.1は「AdjOはウィンドウ"
                "外側の分割・併合を含め比較時点基準に揃える累積調整後の値」と説明しているが、実データでは"
                "分割・併合イベント当日の行自体は『その日より後の分割・併合』のみを反映し当日分の"
                "AdjFactorは当日のAdjO/AdjCには適用されない（＝その日が範囲内最後のイベントなら"
                "AdjO=O・AdjC=Cに数値的に一致する）という規則性が観測された。これがJ-Quantsの"
                "AdjFactor/AdjOの正式な定義上の仕様なのか、それとも§9-9bチェック(b)の想定と"
                "食い違う未確認の挙動なのかは、B実装チームの権限では判定できない（推測で埋めない）。"
            ),
        },
        "fallback_excluded_pairs_if_applied": fallback_excluded_pairs,
        "fallback_excluded_pairs_if_applied_count": len(fallback_excluded_pairs),
        "fallback_excluded_pairs_denominator": (len(r9_9["selection_violations"]) + len(r9_9["confirmation_violations"])) if r9_9 else 0,
        "mapping_ok": mapping_ok,
        "check_c_raw_vs_adj_r5_diff_audit_trail": diff_records,
        "check_c_diff_records_count": len(diff_records),
        "pass": a_pass and b_pass,
        "gate_pass": gate_pass,
        "escalation_required": escalation_required,
        "note": (
            "違反が0件ならfallback_excluded_pairs_if_appliedは空でありG1は全サンプル（選定134,963件・"
            "確認130,235件）をAdj系列のまま使用する。本実測では(b)に274件の違反があり、それを"
            "spec §9-9bの部分的フォールバック手続きどおり機械的に適用すると、9-9で検出した1305件の"
            "(code,week)ペアのうち1050件（80.4%）が除外対象になる。これは§4.3.1が明記する不変条件"
            "「除外は行わずサンプルを一切失わない・サンプル数は不変」と規模の面で正面から矛盾するため、"
            "B実装チームの権限でこのフォールバックを機械的に適用することを見送り、実験を停止してSに"
            "差し戻す（K-6）。fallback_excluded_pairs_if_appliedはSの判断材料として算出済みの値を"
            "そのまま出力している（適用はしていない）。"
        ),
    }


# ---------------------------------------------------------------------------
# 9-10: UL/LL
# ---------------------------------------------------------------------------


def task_9_10(codes: list[str], cal: v2.CalendarV2, log: list[str]) -> dict:
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
    log.append(f"[9-10] total_rows={total_rows} anomalous={sorted(anomalous_values)} missing_rate={missing_rate} gate={gate_pass}")
    return {
        "total_bars_rows_scanned_pinned_range": total_rows,
        "UL_unique_values_and_counts": dict(ul_counter),
        "LL_unique_values_and_counts": dict(ll_counter),
        "combined_missing_rate": missing_rate,
        "anomalous_values": sorted(anomalous_values),
        "values_confined_to_1_0_empty_null": values_confined,
        "gate_pass": gate_pass,
    }


# ---------------------------------------------------------------------------
# params.json
# ---------------------------------------------------------------------------


def build_params(cal, W, r9_1, r9_2, r9_0, feasibility) -> dict:
    return {
        "generated_from": "margin_feasibility.py",
        "spec_reference": "research/EXP-OBS000008/01-spec.md",
        "seed_permutation": 20260917,
        "backup": r9_0,
        "candidate_set": {
            "count": r9_1["count"] if r9_1 else None,
            "reused_determination_dates": r9_1["reused_determination_dates"] if r9_1 else None,
        },
        "T_W_reconstruction": r9_2,
        "buffer_b": BUFFER_B,
        "split_rule": "m_w = floor(N_w/2)",
        "min_events_per_week_for_IC": MIN_EVENTS_PER_WEEK,
        "signal_definition": {
            "primary": "ΔM_w(j) = M_w(j) - M_w'(j)（w'は配列上直前の観測。E-1〜E-4適用後）",
            "sensitivity_only": "M_w(j) = LongVol_w(j)/ShrtVol_w(j)（水準。合否判定には使わない）",
            "exclusion_rules": ["E-1: ShrtVol_w(j)=0", "E-2: ShrtVol_w'(j)=0", "E-3: 暦日差>21日", "E-4: 重複Dateは後勝ち"],
        },
        "pit_rule": {
            "available_date": "T[idx_T(W[w]) + 3]", "entry_date_t1": "T[idx_T(W[w]) + 4]",
            "safety_margin_business_days": 1,
        },
        "prediction_window": {"horizon_primary_H": 5, "entry_price": "O(t1)", "exit_price": "C(t5)"},
        "short_eligibility_field_mapping": feasibility.get("9-4_short_eligibility_field_mapping"),
        "cost_model_summary": {
            "slippage_one_way_pct": 0.00075, "roundtrip_cost_pct_primary": 0.0035,
            "margin_interest_annual_pct_long": 0.028, "lending_fee_annual_pct_short": 0.030,
            "reverse_repo_fee": "モデル化しない（限界開示）",
            "status": "仮置き（spec §8由来）",
        },
        "japan_specific_conditions_status": {
            "A_lot_size_100shares": "9タスク段階では未実施（G2到達時に実装）",
            "B_price_limit_UL_LL": "規約確定済み。9-10で全ユニーク値・欠損率を確認",
            "C_trading_hours": "日足のO=寄付・C=大引けとして対応（G2到達時）",
            "D_earnings_calendar_carryover_rule": "エンドポイント確認のみ実施（9-8）。実装はG2フェーズ",
            "E_margin_requirement_ratio": "未実装（G2フェーズ）",
            "F_credit_regulation": "模擬不能（B-3・HTTP403）。測定範囲の空白として明記",
            "G_margin_interest_rate": "未実装（G2フェーズ。年率2.8%×実保有暦日数/365）",
            "H_short_selling_cost": "未実装（G2フェーズ。貸株料年率3.0%×実保有暦日数/365・逆日歩モデル化なし）",
        },
        "ds_gates_all_pass": feasibility.get("9-3_data_sufficiency_gate", {}).get("all_pass"),
        "can_proceed_to_G1": feasibility.get("can_proceed_to_G1"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
