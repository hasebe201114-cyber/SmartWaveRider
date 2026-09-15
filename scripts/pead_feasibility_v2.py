#!/usr/bin/env python3
"""EXP-OBS000005（PEAD・10年データ版）§9 先行タスク（第2版・§9-0〜§9-7）の実測スクリプト。

spec: `research/EXP-OBS000005/01-spec.md`（第2版）§2.0.1・§9。

**このスクリプトは `data/raw/` を一切書き込まない（読み取り専用）。** §9-0・N-10 により
既存の日足キャッシュ（`data/raw/pead/bars_daily/`）の上書き・再取得・削除は絶対に行わない。

実行順序: 9-0 → 9-1（読み込みのみ・再構成済みの candidate_codes_v2.json を使用）→
T再構成・照合 → 9-2 → universe構築 → 9-3 → 9-5（DSゲート）→ 9-6 → 9-7 → 9-9。
いずれかの必須ゲートが不成立の場合、それ以降は実行せず `stop_reason` を記録する。

判定語は書かない。すべて実測値・件数のみ。

再現用コマンド:
    python3 scripts/pead_feasibility_v2.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gap_common as gc  # noqa: E402
from lib import gap_engine as ge  # noqa: E402
from lib import v2_common as v2  # noqa: E402
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

RESULT_DIR = v2.PEAD_RESULT_DIR
PEAD_RAW_DIR = v2.PEAD_RAW_DIR

# spec §2.1 表の期待値（00-prescreen.md §B-1・spec第2版 §2.0.1 で確認済み）
EXPECTED = {
    "N": 2441, "m": 1220, "T1": "2016-09-15", "TN": "2026-09-14",
    "Tm": "2021-09-15", "T61": "2016-12-15", "Tm_plus_1": "2021-09-16",
}


def main() -> int:  # noqa: C901
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    feasibility: dict = {
        "generated_from": "pead_feasibility_v2.py",
        "spec_reference": "research/EXP-OBS000005/01-spec.md（第2版）§9",
    }
    stop_reason: str | None = None
    escalate_kind: str | None = None

    # ---------------- 9-0: 破壊的操作の禁止・バックアップ確認 ----------------
    backup = v2.verify_backup()
    feasibility["9-0_backup_verification"] = backup
    log.append(f"[9-0] backup_exists={backup['backup_exists']} path={backup['raw_data_backup_path']}")
    if not backup["backup_exists"]:
        stop_reason = "9-0: data/raw/ のバックアップが確認できない"
        escalate_kind = "K-6"

    # ---------------- 9-1: 候補集合（既存の candidate_codes_v2.json を読み込むのみ） ----------------
    codes = v2.load_candidate_codes_v2()
    v2meta = json.loads((RESULT_DIR / "candidate_codes_v2.json").read_text(encoding="utf-8"))
    r9_1 = {
        "candidate_codes_count": len(codes),
        "selection_du_2016-12-15_pass_count": v2meta["selection_du_2016-12-15_pass_count"],
        "confirmation_du_2021-09-16_pass_count": v2meta["confirmation_du_2021-09-16_pass_count"],
        "newly_added_vs_old_511_count": v2meta["newly_added_vs_old_511_count"],
        "old_511_not_in_v2_union_count": v2meta["old_511_not_in_v2_union_count"],
        "note": "既存の candidate_codes_v2.json（EXP-OBS000005/000006共用）を読み込んだのみ。再取得なし",
    }
    feasibility["9-1_candidate_set"] = r9_1
    log.append(f"[9-1] candidate_codes_count={len(codes)}")

    # ---------------- T の再構成（契約範囲ピン留め・spec §2.0.1） ----------------
    cal = v2.CalendarV2(codes)
    N = len(cal.T)
    m = N // 2
    t_checkpoints = {
        "T[1]": cal.at(1), "T[N]": cal.at(N), "T[m]": cal.at(m), "T[m+1]": cal.at(m + 1),
        "T[61]": cal.at(61), "T[m-16]": cal.at(m - 16), "T[N-16]": cal.at(N - 16),
    }
    known_match = (
        N == EXPECTED["N"] and m == EXPECTED["m"] and cal.at(1) == EXPECTED["T1"]
        and cal.at(N) == EXPECTED["TN"] and cal.at(m) == EXPECTED["Tm"]
        and cal.at(61) == EXPECTED["T61"] and cal.at(m + 1) == EXPECTED["Tm_plus_1"]
    )
    t_recon = {
        "contract_range_pin": [v2.CONTRACT_START, v2.CONTRACT_END],
        "N": N, "m": m,
        "missing_bars_codes": cal.missing_bars_codes,
        "checkpoints": t_checkpoints,
        "expected": EXPECTED,
        "matches_expected": known_match,
    }
    feasibility["T_reconstruction"] = t_recon
    log.append(f"[T] N={N} m={m} T[1]={cal.at(1)} T[N]={cal.at(N)} T[m]={cal.at(m)} matches={known_match}")
    if stop_reason is None and not known_match:
        stop_reason = "T再構成がspec §2.0.1の期待値と一致しない"
        escalate_kind = "K-6"

    # ---------------- 9-2: 値の対応付け（D_sel/D_conf 両時点） ----------------
    r9_2 = None
    if stop_reason is None:
        r9_2 = task_9_2(codes, log)
        feasibility["9-2_field_value_mapping"] = r9_2
        if not r9_2["mapping_unambiguous"]:
            stop_reason = "9-2: フィールド値の対応付けが2016年時点と2021年時点で一意に決まらない"
            escalate_kind = "K-6"

    # ---------------- ユニバース構築（D_sel_v2=2016-12-15, D_conf_v2=2021-09-16） ----------------
    sel_universe = conf_universe = None
    if stop_reason is None:
        u_log: list[str] = []
        sel_master_path = v2.PEAD_RAW_DIR / f"master_d_sel_v2_{v2.D_SEL_V2.isoformat()}.json"
        conf_master_path = v2.PEAD_RAW_DIR / f"master_d_conf_v2_{v2.D_CONF_V2.isoformat()}.json"
        sel_universe = ge.build_period_universe("selection", v2.D_SEL_V2, sel_master_path, codes, u_log)
        conf_universe = ge.build_period_universe("confirmation", v2.D_CONF_V2, conf_master_path, codes, u_log)
        for line in u_log:
            log.append(f"[universe] {line}")
        universe_out = {"selection_universe": sel_universe, "confirmation_universe": conf_universe}
        (RESULT_DIR / "universe_v2.json").write_text(
            json.dumps(universe_out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if sel_universe["unresolved_below_min"] or conf_universe["unresolved_below_min"]:
            stop_reason = "universe: U-7緩和後も150銘柄以上のユニバースが成立しない"
            escalate_kind = "K-6"

    # ---------------- 9-3: 欠損営業日率（DS-4） ----------------
    r9_3 = None
    if stop_reason is None:
        r9_3 = task_9_3(cal, codes, log)
        feasibility["9-3_missing_rate"] = r9_3

    # ---------------- SUE計算（9-5/9-6共通） ----------------
    all_events = None
    if stop_reason is None:
        all_events = compute_all_sue_events(codes)

    # ---------------- 9-5: DSゲート（DS-1〜DS-4・C7-1〜C7-3） ----------------
    r9_5 = None
    if stop_reason is None:
        sel_range = (cal.at(61), cal.at(m - 16))
        conf_range = (cal.at(m + 1), cal.at(N - 16))
        n_sel_days = (m - 16) - 61 + 1
        n_conf_days = (N - 16) - (m + 1) + 1
        r9_5 = task_9_5(
            all_events, sel_universe, conf_universe, sel_range, conf_range,
            n_sel_days, n_conf_days, r9_3["overall_missing_rate"], log,
        )
        feasibility["9-5_data_sufficiency_gate"] = r9_5
        if not r9_5["all_pass"]:
            stop_reason = (
                "9-5: データ十分性ゲート(DS-1〜DS-4/C7-1〜C7-3)のいずれかが未達。"
                "K-5に従いSへ差し戻す（判定不能。確認期間のリターンは未見のまま温存）。"
            )
            escalate_kind = "K-5"

    # ---------------- 9-6: 除外内訳・9-7: UL/LL（DSゲート結果に関わらず記録タスクとして実施） ----------------
    if all_events is not None:
        r9_6 = task_9_6(all_events, conf_universe, sel_universe, log)
        feasibility["9-6_exclusion_breakdown"] = r9_6

    r9_7 = task_9_7(codes, log)
    feasibility["9-7_ul_ll_flag_check"] = r9_7
    if stop_reason is None and not r9_7["gate_pass"]:
        stop_reason = "9-7: UL/LLの値がパース規則を満たさない、または欠損率が1%を超える"
        escalate_kind = "K-6"

    # ---------------- 9-9: ローリング窓による先頭欠けの開示 ----------------
    if stop_reason is None:
        r9_9 = task_9_9(cal, codes, sel_universe, log)
        feasibility["9-9_rolling_window_leading_gap"] = r9_9

    feasibility["stop_reason"] = stop_reason
    feasibility["escalate_to_S"] = stop_reason is not None
    feasibility["escalate_kind"] = escalate_kind
    feasibility["can_proceed_to_G1"] = stop_reason is None

    params = build_params(cal, t_recon, r9_2, backup, r9_5)

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
# 9-2: 値の対応付け
# ---------------------------------------------------------------------------


def task_9_2(codes: list[str], log: list[str]) -> dict:
    codes_set = set(codes)

    def tabulate(rows, field):
        c = Counter(r.get(field) for r in rows if r.get("Code") in codes_set)
        return {str(k): v for k, v in sorted(c.items(), key=lambda x: -x[1])}

    sel_master = json.loads(
        (v2.PEAD_RAW_DIR / f"master_d_sel_v2_{v2.D_SEL_V2.isoformat()}.json").read_text(encoding="utf-8")
    )["data"]
    conf_master = json.loads(
        (v2.PEAD_RAW_DIR / f"master_d_conf_v2_{v2.D_CONF_V2.isoformat()}.json").read_text(encoding="utf-8")
    )["data"]

    master_fields = {}
    for field in ["ScaleCat", "Mrgn", "MrgnNm", "S33", "S33Nm"]:
        sel_vals = tabulate(sel_master, field)
        conf_vals = tabulate(conf_master, field)
        master_fields[field] = {
            "d_sel_2016-12-15": sel_vals,
            "d_conf_2021-09-16": conf_vals,
            "value_set_identical": set(sel_vals.keys()) == set(conf_vals.keys()) if field in ("ScaleCat", "Mrgn", "MrgnNm") else None,
            "values_only_in_d_sel": sorted(set(sel_vals.keys()) - set(conf_vals.keys())) if field in ("ScaleCat", "Mrgn", "MrgnNm") else None,
            "values_only_in_d_conf": sorted(set(conf_vals.keys()) - set(sel_vals.keys())) if field in ("ScaleCat", "Mrgn", "MrgnNm") else None,
        }

    # U-1/U-2 マッピングの一意性は「全ユニーク値集合が時点間で完全一致するか」では判定しない
    # （TOPIX指数の区分自体が制度変更で増減しうるため）。判定すべきは
    # 「U-1/U-2が受理する値集合が両時点で存在し、かつその意味が変わっていないか」である。
    # 実測: ScaleCatは2021年時点にのみ 'TOPIX Small 1' が追加で出現する（2016年は無い）。
    # これはTOPIX指数区分自体の変更であり、U-1が受理する
    # {TOPIX Core30, TOPIX Large70, TOPIX Mid400} の集合にもその意味にも影響しない
    # （Small1もSmall2も従来通りU-1の対象外）。Mrgn/MrgnNmは完全一致。
    scalecat_u1_ok_present_both = (
        SCALECAT_U1_OK.issubset(set(master_fields["ScaleCat"]["d_sel_2016-12-15"].keys()))
        and SCALECAT_U1_OK.issubset(set(master_fields["ScaleCat"]["d_conf_2021-09-16"].keys()))
    )
    mrgn_u2_ok_present_both = (
        MRGN_U2_OK.issubset(set(master_fields["Mrgn"]["d_sel_2016-12-15"].keys()))
        and MRGN_U2_OK.issubset(set(master_fields["Mrgn"]["d_conf_2021-09-16"].keys()))
    )

    fins_records = load_fins_summary(codes_filter=codes_set)
    doctype_counter = Counter(r.get("DocType") for r in fins_records)
    curper_counter = Counter(r.get("CurPerType") for r in fins_records)

    # era別（2016-2017年 vs 2021年）でDocType/CurPerTypeの語彙が変わっていないか検査
    era_2016 = [r for r in fins_records if (r.get("DiscDate") or "") < "2018-01-01"]
    era_2021 = [r for r in fins_records if "2021-01-01" <= (r.get("DiscDate") or "") < "2022-01-01"]
    doctype_2016 = set(r.get("DocType") for r in era_2016 if EVENT_DOCTYPE_RE.match(r.get("DocType") or ""))
    doctype_2021 = set(r.get("DocType") for r in era_2021 if EVENT_DOCTYPE_RE.match(r.get("DocType") or ""))
    curper_2016 = set(r.get("CurPerType") for r in era_2016)
    curper_2021 = set(r.get("CurPerType") for r in era_2021)

    event_doctype_values = {k for k in doctype_counter if EVENT_DOCTYPE_RE.match(k or "")}
    unmapped_curper = {k for k in curper_counter if k not in EVENT_CURPERTYPE_OK} - {""}

    mapping_unambiguous = (
        scalecat_u1_ok_present_both
        and mrgn_u2_ok_present_both
        and master_fields["MrgnNm"]["value_set_identical"]
        and doctype_2016.issubset(event_doctype_values)
        and doctype_2021.issubset(event_doctype_values)
    )

    log.append(
        f"[9-2] scalecat_u1_ok_present_both={scalecat_u1_ok_present_both} "
        f"mrgn_u2_ok_present_both={mrgn_u2_ok_present_both} "
        f"scalecat_value_set_identical={master_fields['ScaleCat']['value_set_identical']} "
        f"(values_only_in_d_conf={master_fields['ScaleCat']['values_only_in_d_conf']}) "
        f"doctype_2016_subset={doctype_2016.issubset(event_doctype_values)} "
        f"doctype_2021_subset={doctype_2021.issubset(event_doctype_values)} mapping_unambiguous={mapping_unambiguous}"
    )

    return {
        "master_field_unique_values_by_determination_date": master_fields,
        "fins_summary_scanned_records": len(fins_records),
        "doctype_unique_values_and_counts": {str(k): v for k, v in sorted(doctype_counter.items(), key=lambda x: -x[1])},
        "curpertype_unique_values_and_counts": {str(k): v for k, v in sorted(curper_counter.items(), key=lambda x: -x[1])},
        "era_check_2016_2017_doctype_values": sorted(x for x in doctype_2016 if x),
        "era_check_2021_doctype_values": sorted(x for x in doctype_2021 if x),
        "era_check_2016_2017_curpertype_values": sorted(x for x in curper_2016 if x is not None),
        "era_check_2021_curpertype_values": sorted(x for x in curper_2021 if x is not None),
        "event_doctype_matched_values": sorted(event_doctype_values),
        "curpertype_values_not_in_event_set": sorted(unmapped_curper),
        "mapping_used": {
            "U1_scalecat_topix500_equivalent": sorted(SCALECAT_U1_OK),
            "U2_mrgn_margin_eligible": sorted(MRGN_U2_OK),
            "event_doctype_pattern": EVENT_DOCTYPE_RE.pattern,
            "event_curpertype_values": sorted(EVENT_CURPERTYPE_OK),
        },
        "scalecat_vocabulary_note": (
            "ScaleCatの全ユニーク値集合は2016-12-15時点と2021-09-16時点で完全には一致しない"
            "（2021-09-16時点にのみ 'TOPIX Small 1' が出現。2016-12-15時点は 'TOPIX Small 2' と "
            "'-' のみでSmall1相当の区分が無い）。これはTOPIX指数区分自体の制度変更であり、"
            "U-1が受理する集合 {TOPIX Core30, TOPIX Large70, TOPIX Mid400} にもその意味にも"
            "影響しない（Small1・Small2ともに常にU-1の対象外）ため、mapping_unambiguousの"
            "判定には用いていない。curpertype '4Q' はEVENT_CURPERTYPE_OKに含まれない値として"
            "両時点に存在するが、これは元spec（EXP-OBS000001）から継続する既知の非イベント値で"
            "あり、is_event_disclosure()が要求するCurPerType∈{1Q,2Q,3Q,FY}の判定から機械的に"
            "除外される（curpertype_values_not_in_event_setに記録済み）。"
        ),
        "mapping_unambiguous": mapping_unambiguous,
    }


# ---------------------------------------------------------------------------
# 9-3: 欠損営業日率
# ---------------------------------------------------------------------------


def task_9_3(cal: v2.CalendarV2, codes: list[str], log: list[str]) -> dict:
    total_expected = 0
    total_actual = 0
    total_missing = 0
    per_code_detail = []
    for code in codes:
        dates = cal.bars_by_code.get(code, {})
        if not dates:
            continue
        code_min, code_max = min(dates), max(dates)
        expected_days = [d for d in cal.T if code_min <= d <= code_max]
        expected = len(expected_days)
        actual = len(dates)
        missing = expected - actual
        total_expected += expected
        total_actual += actual
        total_missing += missing
        if missing > 0:
            per_code_detail.append(
                {"code": code, "active_range": [code_min, code_max], "expected": expected,
                 "actual": actual, "missing": missing, "missing_rate": missing / expected if expected else None}
            )
    overall_missing_rate = (total_missing / total_expected) if total_expected else None
    worst = sorted(per_code_detail, key=lambda r: -(r["missing_rate"] or 0))[:20]
    log.append(f"[9-3] overall_missing_rate={overall_missing_rate} codes_with_any_missing={len(per_code_detail)}")
    return {
        "candidate_codes_count": len(codes),
        "codes_with_bars": len(cal.bars_by_code),
        "total_expected_code_days": total_expected,
        "total_actual_code_days": total_actual,
        "total_missing_code_days": total_missing,
        "overall_missing_rate": overall_missing_rate,
        "worst_20_codes_by_missing_rate": worst,
        "methodology": (
            "契約範囲でピン留めしたTを参照営業日カレンダーとし、各銘柄について"
            "自身の最初の観測日〜最後の観測日（ピン留め後）の範囲内にあるT上の日のうち、"
            "実際にその銘柄の行が存在しない日数を欠損とした（上場前/廃止後の期間・"
            "ローリング窓により失われた先頭日は分母に含めない＝missing扱いにしない）。"
        ),
    }


# ---------------------------------------------------------------------------
# SUE計算（9-5/9-6共通）
# ---------------------------------------------------------------------------


def compute_all_sue_events(codes: list[str]) -> list[dict]:
    fins_records = load_fins_summary(codes_filter=set(codes))
    by_code = build_all_disclosures_index(fins_records)
    all_events = []
    for code, disclosures in by_code.items():
        all_events.extend(compute_raw_sue_for_code(disclosures))
    return all_events


# ---------------------------------------------------------------------------
# 9-5: DSゲート
# ---------------------------------------------------------------------------


def task_9_5(all_events, sel_universe, conf_universe, sel_range, conf_range, n_sel_days, n_conf_days, missing_rate, log) -> dict:
    valid_events = [e for e in all_events if e["raw_sue"] is not None]
    sel_codes = set(sel_universe["codes"])
    conf_codes = set(conf_universe["codes"])

    def in_range(e, codes_set, lo, hi):
        return e["code"] in codes_set and lo <= e["disc_date"] <= hi

    sel_events = [e for e in valid_events if in_range(e, sel_codes, *sel_range)]
    conf_events = [e for e in valid_events if in_range(e, conf_codes, *conf_range)]

    sel_by_day = Counter(e["disc_date"] for e in sel_events)
    conf_by_day = Counter(e["disc_date"] for e in conf_events)

    ds1_count = len(conf_events)
    ds1_pass = ds1_count >= 300

    ds2_days = sum(1 for c in conf_by_day.values() if c >= 5)
    ds2_pass = ds2_days >= 30

    ds3_count = conf_universe["final_count"]
    ds3_pass = ds3_count >= 150

    ds4_pass = (missing_rate is not None) and (missing_rate <= 0.05)

    c7_1_pass = ds2_days >= 30  # 同一量（DS-2と同一）

    max_day_count = max(conf_by_day.values()) if conf_by_day else 0
    max_day_date = max(conf_by_day, key=lambda d: conf_by_day[d]) if conf_by_day else None
    c7_2_share = (max_day_count / ds1_count) if ds1_count else None
    c7_2_pass = c7_2_share is not None and c7_2_share <= 0.25

    rate_sel = len(sel_events) / n_sel_days if n_sel_days else None
    rate_conf = len(conf_events) / n_conf_days if n_conf_days else None
    c7_3_ratio = (max(rate_sel, rate_conf) / min(rate_sel, rate_conf)) if (rate_sel and rate_conf) else None
    c7_3_pass = c7_3_ratio is not None and c7_3_ratio <= 2.0

    all_pass = ds1_pass and ds2_pass and ds3_pass and ds4_pass and c7_1_pass and c7_2_pass and c7_3_pass

    log.append(
        f"[9-5] DS-1={ds1_count}:{ds1_pass} DS-2={ds2_days}:{ds2_pass} DS-3={ds3_count}:{ds3_pass} "
        f"DS-4={missing_rate}:{ds4_pass} C7-1={ds2_days}:{c7_1_pass} C7-2={c7_2_share}:{c7_2_pass} "
        f"C7-3={c7_3_ratio}:{c7_3_pass} ALL_PASS={all_pass}"
    )

    return {
        "selection_event_range": list(sel_range),
        "confirmation_event_range": list(conf_range),
        "selection_event_object_venue_business_days": n_sel_days,
        "confirmation_event_object_business_days": n_conf_days,
        "selection_event_count": len(sel_events),
        "confirmation_event_count": len(conf_events),
        "DS-1_confirmation_valid_event_count": {"value": ds1_count, "threshold_min": 300, "pass": ds1_pass},
        "DS-2_days_with_ge5_events": {"value": ds2_days, "threshold_min": 30, "pass": ds2_pass},
        "DS-3_confirmation_universe_count": {"value": ds3_count, "threshold_min": 150, "pass": ds3_pass},
        "DS-4_overall_missing_rate": {"value": missing_rate, "threshold_max": 0.05, "pass": ds4_pass},
        "C7-1_distinct_event_days": {"value": ds2_days, "threshold_min": 30, "pass": c7_1_pass},
        "C7-2_max_single_day_share": {
            "value": c7_2_share, "max_day_date": max_day_date, "max_day_count": max_day_count,
            "threshold_max": 0.25, "pass": c7_2_pass,
        },
        "C7-3_selection_confirmation_rate_ratio": {
            "rate_selection": rate_sel, "rate_confirmation": rate_conf,
            "value": c7_3_ratio, "threshold_max": 2.0, "pass": c7_3_pass,
        },
        "all_pass": all_pass,
    }


# ---------------------------------------------------------------------------
# 9-6: 除外内訳（A(t)欠測/B(t)不在/Scale(t)欠測・≤0/会計年度不一致/重複開示集約）
# ---------------------------------------------------------------------------


def task_9_6(all_events, conf_universe, sel_universe, log) -> dict:
    fine_reason = Counter()
    for e in all_events:
        if e["raw_sue"] is not None:
            continue
        reason = e["excluded_reason"]
        fine_reason[reason] += 1

    total_events = len(all_events)
    valid_count = sum(1 for e in all_events if e["raw_sue"] is not None)
    excluded_total = total_events - valid_count

    conf_codes = set(conf_universe["codes"])
    sel_codes = set(sel_universe["codes"])
    excluded_codes_conf = {e["code"] for e in all_events if e["raw_sue"] is None and e["code"] in conf_codes}

    conf_details = {r["code"]: r for r in conf_universe["details"]}
    scalecat_dist = Counter(conf_details[c]["scale_cat"] for c in excluded_codes_conf if c in conf_details)
    s33_dist = Counter(conf_details[c]["s33"] for c in excluded_codes_conf if c in conf_details)

    log.append(f"[9-6] excluded_total={excluded_total} by_reason={dict(fine_reason)}")

    return {
        "total_events_scanned": total_events,
        "valid_raw_sue_count": valid_count,
        "excluded_total": excluded_total,
        "excluded_count_by_reason": {
            "B_t_absent_or_fy_mismatch_no_matching_prior_disclosure": fine_reason.get("no_matching_prior_disclosure", 0),
            "null_or_nonnumeric_input_A_B_or_Scale": fine_reason.get("null_or_nonnumeric_input", 0),
            "scale_le_zero": fine_reason.get("scale_le_zero", 0),
        },
        "note_on_granularity": (
            "pead_common.compute_raw_sue_for_code は A(t)欠測とB(t)/Scale(t)欠測を "
            "'null_or_nonnumeric_input' に合算して返す実装のため、これ以上の細分（A単独/B単独/Scale単独）"
            "は本スクリプトでは分離していない。'no_matching_prior_disclosure' はspec の「B(t)不在（同一会計年度の"
            "直前開示なし）」に相当し、会計年度不一致もこのカテゴリに含まれる（同一会計年度に一致する直前開示が"
            "存在しないという点で同一の判定だから）。"
        ),
        "excluded_codes_in_confirmation_universe_count": len(excluded_codes_conf),
        "excluded_codes_scalecat_distribution": dict(scalecat_dist),
        "excluded_codes_s33_distribution": dict(s33_dist),
    }


# ---------------------------------------------------------------------------
# 9-7: UL/LL
# ---------------------------------------------------------------------------


def task_9_7(codes: list[str], log: list[str]) -> dict:
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
    log.append(f"[9-7] total_rows={total_rows} anomalous={sorted(anomalous_values)} missing_rate={missing_rate} gate={gate_pass}")
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
# 9-9: ローリング窓による先頭欠けの開示
# ---------------------------------------------------------------------------


def task_9_9(cal: v2.CalendarV2, codes: list[str], sel_universe: dict, log: list[str]) -> dict:
    v2meta = json.loads((RESULT_DIR / "candidate_codes_v2.json").read_text(encoding="utf-8"))
    new_codes = set(v2meta["newly_added_vs_old_511_codes"])

    later_start_codes = []
    for c in codes:
        first = cal.raw_first_date.get(c)
        if first is not None and first > v2.CONTRACT_START:
            later_start_codes.append(
                {
                    "code": c,
                    "raw_first_date": first,
                    "raw_last_date": cal.raw_last_date.get(c),
                    "in_newly_added_90": c in new_codes,
                    "likely_cause": "rolling_window_fetched_today" if c in new_codes else "genuine_later_listing_or_other",
                }
            )
    later_start_codes.sort(key=lambda r: r["raw_first_date"])

    # (b) U-4助走窓 T[1..60] での Va 個数損失（選定ユニバースの構築に使ったコード集合限定）
    T_window = cal.T[0:60]  # T[1..60]
    va_loss_detail = []
    for entry in later_start_codes:
        c = entry["code"]
        bd = cal.bars_by_code.get(c, {})
        count_in_window = sum(1 for d in T_window if d in bd and bd[d].get("Va") is not None)
        va_loss_detail.append({"code": c, "va_count_in_U4_window_T1_T60": count_in_window})

    # (c) U-4「有効Va30個未満」で不通過となった銘柄数（選定ユニバース構築ログから）
    u4_below_30_count = sum(1 for r in va_loss_detail if r["va_count_in_U4_window_T1_T60"] < 30)

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
            "個々のAPI呼び出しタイムスタンプはログに残していない（ファイルmtimeはコンテナ再展開により"
            "全ファイル同一日を示し信頼できない）。raw_first_date が契約開始日2016-09-15と一致するかどうかを"
            "代理指標として使い、'in_newly_added_90'（本セッションでcandidate_codes_v2.json構築時に新規取得と"
            "記録した90銘柄のリストとの一致）と突き合わせている。"
        ),
        "va_count_in_U4_window_detail": va_loss_detail,
        "U4_valid_va_below_30_count": u4_below_30_count,
        "U4_existing_rule_threshold": 30,
        "note": "新しい閾値は導入していない。既存のU-4規則（有効Va30個未満は不通過）で機械的に処理されることの確認のみ",
    }


# ---------------------------------------------------------------------------
# params.json
# ---------------------------------------------------------------------------


def build_params(cal, t_recon, r9_2, backup, r9_5) -> dict:
    return {
        "generated_from": "pead_feasibility_v2.py",
        "spec_reference": "research/EXP-OBS000005/01-spec.md（第2版）",
        "seed": 20260915,
        "raw_data_backup_path": backup["raw_data_backup_path"],
        "raw_data_backup_verified": backup["backup_exists"],
        "t_contract_range_pin": [v2.CONTRACT_START, v2.CONTRACT_END],
        "T_reconstruction": t_recon,
        "field_value_mapping_9_2": r9_2,
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
            "entry_price": "O(t1)（DiscDateの翌営業日の始値）",
            "entry_slippage": 0.00075,
            "gross_return_formula": "R_5(j) = C(T[i+5]) / O(T[i+1]) - 1",
        },
        "trailing_stop_params": {
            "initial_stop_pct": -0.03, "trail_activation_pct": 0.04, "trail_follow_pct": -0.02,
            "update_and_fill_basis": "日足終値のみで更新・抵触判定。抵触翌営業日始値で約定",
            "max_holding_business_days": 10, "forced_carry_over_max_days": 5,
        },
        "position_sizing": {
            "capital_jpy": 1_000_000, "leverage": 2.0, "position_cap_jpy": 2_000_000,
            "unit_shares": 100,
        },
        "universe_rules": {
            "U1_scalecat": sorted(SCALECAT_U1_OK), "U2_mrgn_ok": sorted(MRGN_U2_OK),
            "U4_liquidity_min_va_jpy": 5e8, "U4_window_business_days": 60,
            "U5_price_band_main": [1000, 3500], "U6_max_universe": 175, "U7_min_universe": 150,
            "U7_price_band_fallback": [700, 8000],
        },
        "cost_model_summary": {
            "slippage_one_way_pct": 0.00075, "roundtrip_cost_pct_primary": 0.0035,
            "roundtrip_cost_sensitivity": [0.0025, 0.0035, 0.0050],
            "margin_interest_annual_pct": 0.028, "status": "仮置き（EXP-OBS000001由来を継承）",
        },
        "japan_specific_conditions_status": {
            "A_lot_size_100shares": "9タスク段階では未実施（G2到達時に実装）",
            "B_price_limit_UL_LL": "規約確定済み（§4.0.4の2式）。9-7で全ユニーク値・欠損率を確認",
            "C_trading_hours": "日足のO=寄付・C=大引けとして対応（G2到達時）",
            "D_earnings_calendar_carryover_rule": "未実装（G2フェーズ）。/fins/earnings-date を使用予定",
            "E_margin_requirement_ratio": "未実装（G2フェーズ）",
            "F_credit_regulation": "模擬不能（C-5・HTTP403）。測定範囲の空白として明記",
            "G_margin_interest_rate": "未実装（G2フェーズ。年率2.8%×実保有暦日数/365）",
            "H_short_selling": "該当なし（ロングオンリー）",
        },
        "ds_gates_all_pass": r9_5["all_pass"] if r9_5 else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
