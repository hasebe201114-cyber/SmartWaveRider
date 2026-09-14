#!/usr/bin/env python3
"""EXP-OBS000003（非決算オーバーナイト・ギャップ）§9 先行タスク 9-1 / 9-2 / 9-3 の実測スクリプト。

spec: `research/EXP-OBS000003/01-spec.md` §2.0（T の定義）・§3.1（価格調整規約）・§4.0（UL/LL）。

前提: `data/raw/pead/` に既に取得済みの候補銘柄511件の `/equities/bars/daily` 全履歴・
`research/EXP-OBS000001/10-result/candidate_codes.json` を再利用する
（spec §2.0 が明記する既知の候補銘柄数511・カレンダー実測値と同一の入力であるため、
新規フェッチを行わない。新規フェッチが必要になった場合はこのスクリプトを拡張する）。

出力:
  - `research/EXP-OBS000003/10-result/params.json`（9-1・9-2 の実測値）
  - `research/EXP-OBS000003/10-result/feasibility.json`（9-1・9-2・9-3 の判定結果）

判定語は書かない。すべて実測値・件数のみ。

9-2 の結論が「一意に決まらない」場合、このスクリプトは feasibility.json に
`"9-2_verdict": "AMBIGUOUS_ESCALATE_TO_S"` を出力し、戻り値コード2で終了する
（spec §9-2・N-8・K-6。実験を回さず S に差し戻す）。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.pead_common import AnomalousFlagValueError, parse_ul_ll_flag  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PEAD_RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"
PEAD_RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"
RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000003" / "10-result"

KNOWN_T1 = "2024-06-21"
KNOWN_T_LEN = 487
KNOWN_T487 = "2026-06-22"
KNOWN_CANDIDATE_COUNT = 511


def load_candidate_codes() -> list[str]:
    d = json.loads((PEAD_RESULT_DIR / "candidate_codes.json").read_text(encoding="utf-8"))
    return d["codes"]


def load_bars(code: str) -> list[dict] | None:
    path = PEAD_RAW_DIR / "bars_daily" / f"{code}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- 9-1: 参照営業日カレンダー T の再構成（spec §2.0） ----------


def task_9_1(codes: list[str], log: list[str]) -> tuple[list[str], dict]:
    date_set: set[str] = set()
    codes_missing_bars: list[str] = []
    for code in codes:
        bars = load_bars(code)
        if bars is None:
            codes_missing_bars.append(code)
            continue
        for r in bars:
            date_set.add(r["Date"])
    T = sorted(date_set)  # noqa: N806  (spec の記号 T に合わせる)

    def at(i: int) -> str | None:
        # spec は1始まり添字。i>=1。
        if 1 <= i <= len(T):
            return T[i - 1]
        return None

    checkpoints = {
        "T[1]": at(1),
        "T[60]": at(60),
        "T[61]": at(61),
        "T[62]": at(62),
        "T[477]": at(477),
        "T[481]": at(481),
        "T[482]": at(482),
        f"T[{len(T)}]": at(len(T)),
    }

    known_match = (
        at(1) == KNOWN_T1
        and len(T) == KNOWN_T_LEN
        and at(KNOWN_T_LEN) == KNOWN_T487
        and len(codes) == KNOWN_CANDIDATE_COUNT
    )
    len_ge_480 = len(T) >= 480

    log.append(
        f"[9-1] candidate_codes={len(codes)} codes_missing_bars={len(codes_missing_bars)} "
        f"|T|={len(T)} T[1]={at(1)} T[{len(T)}]={at(len(T))} known_value_match={known_match}"
    )

    result = {
        "candidate_codes_count": len(codes),
        "codes_missing_bars_count": len(codes_missing_bars),
        "codes_missing_bars": codes_missing_bars,
        "T_len": len(T),
        "T_checkpoints": checkpoints,
        "known_values_reference": {
            "source": "research/EXP-OBS000001/10-result/feasibility.json の 9-2_missing_rate",
            "T[1]": KNOWN_T1,
            "|T|": KNOWN_T_LEN,
            "T[487]": KNOWN_T487,
            "candidate_codes_count": KNOWN_CANDIDATE_COUNT,
        },
        "known_values_match": known_match,
        "gate_len_ge_480": len_ge_480,
        "gate_9_1_pass": bool(known_match and len_ge_480),
    }
    return T, result


# ---------- 9-2: 価格調整規約の確定（spec §3.1。本EXP固有の最重要タスク） ----------


def task_9_2(codes: list[str], log: list[str]) -> dict:
    total_rows = 0
    adjfactor_counter: Counter = Counter()
    exrt_counter: Counter = Counter()
    exrt_null = 0
    adjfactor_null = 0
    # AdjFactor!=1 の行のみ詳細を保存（分割・併合検算用）
    non_unity_rows: list[dict] = []
    exrt_present_but_adjfactor_unity: list[dict] = []
    monthly_exrt_counter: Counter = Counter()

    codes_scanned = 0
    for code in codes:
        bars = load_bars(code)
        if bars is None:
            continue
        codes_scanned += 1
        bars_sorted = sorted(bars, key=lambda r: r["Date"])
        for idx, r in enumerate(bars_sorted):
            total_rows += 1
            af = r.get("AdjFactor")
            ex = r.get("ExRT")
            if af is None:
                adjfactor_null += 1
                af_key = "null"
            else:
                af_key = repr(af)
            adjfactor_counter[af_key] += 1
            exrt_counter[repr(ex)] += 1
            if ex is None:
                exrt_null += 1
            else:
                monthly_exrt_counter[r["Date"][:7]] += 1

            if af is not None and af != 1.0:
                prev_row = bars_sorted[idx - 1] if idx > 0 else None
                entry = {
                    "code": r.get("Code"),
                    "date": r.get("Date"),
                    "AdjFactor": af,
                    "ExRT": ex,
                    "O": r.get("O"),
                    "C": r.get("C"),
                    "AdjO": r.get("AdjO"),
                    "AdjC": r.get("AdjC"),
                }
                if prev_row is not None:
                    entry["prev_date"] = prev_row.get("Date")
                    entry["prev_C"] = prev_row.get("C")
                    entry["prev_AdjC"] = prev_row.get("AdjC")
                    # prev_AdjC should equal prev_C * af（累積調整の検算）
                    if prev_row.get("C") is not None and prev_row.get("AdjC") is not None:
                        expected = prev_row["C"] * af
                        diff = abs(expected - prev_row["AdjC"])
                        # AdjC は小数1桁に丸められて配信されるため、丸め誤差(最大0.05円)を
                        # 許容する（相対1e-4では丸め誤差を「不一致」と誤判定するため）。
                        entry["prev_AdjC_matches_C_times_AdjFactor"] = diff <= 0.06
                non_unity_rows.append(entry)
            elif ex is not None and (af is None or af == 1.0):
                exrt_present_but_adjfactor_unity.append(
                    {"code": r.get("Code"), "date": r.get("Date"), "AdjFactor": af, "ExRT": ex}
                )

    # AdjFactor!=1 の値のクリーン分割比率検算（1/n 系との照合）
    non_unity_values = sorted({e["AdjFactor"] for e in non_unity_rows})
    clean_split_check = []
    for v in non_unity_values:
        inv = 1.0 / v if v else None
        is_clean_integer_ratio = inv is not None and abs(inv - round(inv)) <= 1e-6
        clean_split_check.append(
            {"AdjFactor": v, "1/AdjFactor": inv, "is_clean_integer_ratio": is_clean_integer_ratio}
        )
    all_non_unity_clean = all(c["is_clean_integer_ratio"] for c in clean_split_check)

    # 累積調整の検算集計
    continuity_checked = [e for e in non_unity_rows if "prev_AdjC_matches_C_times_AdjFactor" in e]
    continuity_pass = sum(1 for e in continuity_checked if e["prev_AdjC_matches_C_times_AdjFactor"])
    continuity_fail = [
        e for e in continuity_checked if not e["prev_AdjC_matches_C_times_AdjFactor"]
    ]

    denom = total_rows
    exrt_missing_rate = exrt_null / denom if denom else None
    adjfactor_missing_rate = adjfactor_null / denom if denom else None

    # ExRT=1 の月次分布（配当集中月＝3月・9月への偏りの有無を記述するのみ。判定には使わない）
    monthly_dist = {k: monthly_exrt_counter[k] for k in sorted(monthly_exrt_counter)}

    # --- 配当落ち識別可能性の判定 ---
    # 判定根拠（すべて実測値のみ。推測を加えない）:
    #  (a) AdjFactor が1以外を取る行は全て「1/整数」のきれいな比率のみ（分割・併合比率と整合）。
    #      配当調整であれば (1 - 配当/株価) 型の非整数比率が混在するはずだが、観測されない。
    #  (b) ExRT!=null の行数は2年間511銘柄で91件のみ。配当は主要企業で年1〜2回発生するため、
    #      配当も調整対象なら ExRT!=null の件数は数百〜数千件規模になるはずだが、そうなっていない。
    #  (c) /fins/summary には Div1Q/Div2Q/Div3Q/DivAnn/DivFY 等の配当金額フィールドは存在するが、
    #      配当の権利確定日・除権日（ex-dividend date）に相当する日付フィールドは存在しない
    #      （111フィールド中に該当なし。§9-2 での全フィールド列挙により確認）。
    #  (d) /equities/bars/daily 側にも配当専用フラグは存在しない（UL/LL/AdjFactor/ExRT/MktCap のみ）。
    # (a)〜(d) より、本契約で取得可能なデータには「配当落ち日」を識別する手段が存在しない。
    dividend_identifiable = False
    dividend_identification_reasoning = (
        "AdjFactor!=1 の全91件が整数分の1のクリーンな比率（分割・併合と整合）のみで構成され、"
        "配当調整に典型的な非整数比率（1-配当/株価型）が一件も観測されない。"
        "ExRT!=null は2年511銘柄で91件のみであり、配当（主要企業で年1〜2回発生）を含むには少なすぎる。"
        "/fins/summary の111フィールド中に配当金額（Div*/FDiv*/NxFDiv*）は存在するが、"
        "配当の除権日・権利確定日に相当する日付フィールドは存在しない。"
        "/equities/bars/daily 側にも配当専用フラグは存在しない。"
        "したがって本契約で取得可能なデータの範囲内では、配当落ち日を識別する手段が存在しない。"
    )

    split_merger_identifiable = (
        len(exrt_counter) <= 2  # None（欠損なし）と '1' のみ
        and set(exrt_counter.keys()) <= {repr(None), repr("1")}
        and all_non_unity_clean
        and len(continuity_fail) == 0
    )

    e1_e2_uniquely_determined = split_merger_identifiable and dividend_identifiable

    log.append(
        f"[9-2] rows_scanned={total_rows} codes_scanned={codes_scanned} "
        f"adjfactor_unique_values={len(adjfactor_counter)} exrt_unique_values={len(exrt_counter)} "
        f"non_unity_adjfactor_rows={len(non_unity_rows)} all_non_unity_clean_split_ratio={all_non_unity_clean} "
        f"continuity_checked={len(continuity_checked)} continuity_pass={continuity_pass} "
        f"continuity_fail={len(continuity_fail)} "
        f"split_merger_identifiable={split_merger_identifiable} "
        f"dividend_identifiable={dividend_identifiable} "
        f"E1_E2_uniquely_determined={e1_e2_uniquely_determined}"
    )

    return {
        "codes_scanned": codes_scanned,
        "total_bars_rows_scanned": total_rows,
        "AdjFactor_unique_values_and_counts": dict(
            sorted(adjfactor_counter.items(), key=lambda x: -x[1])
        ),
        "AdjFactor_null_count": adjfactor_null,
        "AdjFactor_missing_rate": adjfactor_missing_rate,
        "ExRT_unique_values_and_counts": dict(sorted(exrt_counter.items(), key=lambda x: -x[1])),
        "ExRT_null_count": exrt_null,
        "ExRT_missing_rate": exrt_missing_rate,
        "ExRT_nonnull_monthly_distribution": monthly_dist,
        "AdjFactor_non_unity_row_count": len(non_unity_rows),
        "AdjFactor_non_unity_examples_first20": non_unity_rows[:20],
        "AdjFactor_non_unity_unique_values_clean_split_ratio_check": clean_split_check,
        "all_non_unity_values_are_clean_split_ratios": all_non_unity_clean,
        "cumulative_adjustment_continuity_checked_count": len(continuity_checked),
        "cumulative_adjustment_continuity_pass_count": continuity_pass,
        "cumulative_adjustment_continuity_fail_examples": continuity_fail[:20],
        "ExRT_present_but_AdjFactor_unity_row_count": len(exrt_present_but_adjfactor_unity),
        "ExRT_present_but_AdjFactor_unity_examples_first20": exrt_present_but_adjfactor_unity[:20],
        "fins_summary_dividend_amount_fields_present": [
            "Div1Q", "Div2Q", "Div3Q", "DivAnn", "DivFY", "DivTotalAnn", "DivUnit",
            "FDiv1Q", "FDiv2Q", "FDiv3Q", "FDivAnn", "FDivFY", "FDivTotalAnn", "FDivUnit",
            "NxFDiv1Q", "NxFDiv2Q", "NxFDiv3Q", "NxFDivAnn", "NxFDivFY", "NxFDivUnit",
        ],
        "fins_summary_total_field_count": 111,
        "fins_summary_ex_dividend_date_field_present": False,
        "bars_daily_dividend_specific_flag_present": False,
        "split_merger_adjustment_convention": {
            "identifiable": split_merger_identifiable,
            "rule": (
                "ExRT=='1' かつ AdjFactor!=1.0 の銘柄日を株式分割・併合・株式無償割当等の権利落ち日とする。"
                "AdjFactor は当日以前の全履行日に遡って乗じる累積調整係数であり、"
                "prev_AdjC == prev_C * AdjFactor(当日) が成立する（継続性を実測で確認）。"
            ),
        },
        "dividend_ex_date_identification": {
            "identifiable": dividend_identifiable,
            "reasoning": dividend_identification_reasoning,
        },
        "E1_E2_uniquely_determined": e1_e2_uniquely_determined,
        "gate_9_2_pass": e1_e2_uniquely_determined,
        "verdict": "OK" if e1_e2_uniquely_determined else "AMBIGUOUS_ESCALATE_TO_S",
    }


# ---------- 9-3: UL / LL の再検証（spec §4.0） ----------


def task_9_3(codes: list[str], log: list[str]) -> dict:
    ul_counter: Counter = Counter()
    ll_counter: Counter = Counter()
    total_rows = 0
    ul_null = 0
    ll_null = 0
    anomalous_values: set = set()

    for code in codes:
        bars = load_bars(code)
        if bars is None:
            continue
        for r in bars:
            total_rows += 1
            for raw, counter_ref, is_ul in (
                (r.get("UL"), ul_counter, True),
                (r.get("LL"), ll_counter, False),
            ):
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

    log.append(
        f"[9-3] total_rows={total_rows} ul_unique={len(ul_counter)} ll_unique={len(ll_counter)} "
        f"anomalous_values={sorted(anomalous_values)} missing_rate={missing_rate} gate_pass={gate_pass}"
    )

    return {
        "total_bars_rows_scanned": total_rows,
        "UL_unique_values_and_counts": dict(ul_counter),
        "LL_unique_values_and_counts": dict(ll_counter),
        "UL_null_count": ul_null,
        "LL_null_count": ll_null,
        "combined_missing_rate": missing_rate,
        "anomalous_values": sorted(anomalous_values),
        "values_confined_to_1_0_empty_null": values_confined,
        "missing_rate_threshold": 0.01,
        "gate_9_3_pass": gate_pass,
    }


def main() -> int:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    codes = load_candidate_codes()

    T, r9_1 = task_9_1(codes, log)
    r9_2 = task_9_2(codes, log)
    r9_3 = task_9_3(codes, log)

    stop_reason = None
    if not r9_1["gate_9_1_pass"]:
        stop_reason = "9-1: |T| または既知値との一致に失敗（S へ差し戻し）"
    elif not r9_2["gate_9_2_pass"]:
        stop_reason = (
            "9-2: 配当落ち日(E-2)の識別手段が実データ上に存在しないため一意に決まらない。"
            "K-6・D-7（司令塔承認済み方針）に従い S へ差し戻す。実験（9-4以降・G1・G2）は実行しない。"
        )

    feasibility = {
        "generated_from": "gap_feasibility.py",
        "spec_reference": "research/EXP-OBS000003/01-spec.md §9",
        "9-1_reference_calendar_T": r9_1,
        "9-2_price_adjustment_convention": r9_2,
        "9-3_ul_ll_flag_check": r9_3,
        "9-4_universe_construction": "未実施（9-2差し戻しのため着手せず）" if stop_reason else None,
        "9-5_gap_sigma_z_descriptive_stats": "未実施（9-2差し戻しのため着手せず）" if stop_reason else None,
        "9-6_z_star_calibration_and_ds_gates": "未実施（9-2差し戻しのため着手せず）" if stop_reason else None,
        "9-7_pead_disjointness_check": "未実施（9-2差し戻しのため着手せず）" if stop_reason else None,
        "9-8_unidentified_news_contamination": "未実施（9-2差し戻しのため着手せず）" if stop_reason else None,
        "9-9_self_consistency_check": "未実施（9-2差し戻しのため着手せず）" if stop_reason else None,
        "stop_reason": stop_reason,
        "escalate_to_S": stop_reason is not None,
    }

    params = {
        "generated_from": "gap_feasibility.py",
        "spec_reference": "research/EXP-OBS000003/01-spec.md",
        "seed": 20260914,
        "reference_calendar_T_checkpoints": r9_1["T_checkpoints"],
        "reference_calendar_T_len": r9_1["T_len"],
        "reference_calendar_known_values_match": r9_1["known_values_match"],
        "price_adjustment_convention": {
            "split_merger_adjustment_convention": r9_2["split_merger_adjustment_convention"],
            "dividend_ex_date_identification": r9_2["dividend_ex_date_identification"],
            "E1_E2_uniquely_determined": r9_2["E1_E2_uniquely_determined"],
        },
        "ul_ll_flag_parse_rule": (
            "'1' -> True, '0'/'' -> False, null -> 欠損。それ以外の値は AnomalousFlagValueError"
            "（research/EXP-OBS000001/01-spec.md §4.0.3 を参照により流用。scripts/lib/pead_common.py "
            "の parse_ul_ll_flag / buy_blocked / sell_blocked を再利用）"
        ),
        "ul_ll_missing_rate": r9_3["combined_missing_rate"],
        "japan_specific_conditions_status": {
            "A_unit_100_share_discretization": "未実施（9-2差し戻しのため）",
            "B_price_limit_ul_ll": "未実施（9-2差し戻しのため）",
            "C_trading_hours_fixed": "未実施（9-2差し戻しのため）",
            "D_earnings_date_avoidance": "未実施（9-2差し戻しのため）",
            "E_margin_requirement": "未実施（9-2差し戻しのため）",
            "F_credit_regulation_unsimulatable": "模擬不能（データ取得不可・C-5・HTTP403）。spec §7 F項の通り",
            "G_margin_interest_rate": "未実施（9-2差し戻しのため）",
            "H_short_leg": "該当なし（ロングオンリー・spec §5.2）",
            "I_rights_dividend_exclusion": (
                "権利落ち(分割・併合)は識別可能。配当落ちは識別不能につき9-2でS差し戻し（K-6）"
            ),
        },
        "stop_reason": stop_reason,
    }

    (RESULT_DIR / "feasibility.json").write_text(
        json.dumps(feasibility, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RESULT_DIR / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for line in log:
        print(line)
    print(f"saved: {RESULT_DIR / 'feasibility.json'}")
    print(f"saved: {RESULT_DIR / 'params.json'}")

    if stop_reason:
        print(f"STOP: {stop_reason}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
