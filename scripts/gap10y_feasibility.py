#!/usr/bin/env python3
"""EXP-OBS000008（ギャップ・10年版）§9 先行タスクと §6.0 データ十分性ゲート（DS-1〜DS-8）。

前提: `jq10y_build_db.py`・`jq10y_compute_calendar.py`・`jq10y_build_universe.py`（PEAD側と共有）・
`jq10y_common_tasks.py` が完了していること。

出力:
  - `research/EXP-OBS000008/10-result/feasibility.json`
  - `research/EXP-OBS000008/10-result/params.json`

**リターンを一切参照しない。** z* 較正は選定期間の候補件数のみを入力とする（§3.6）。
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jq10y_common import DB_PATH, Calendar, UniverseIndex, load_calendar, load_universe, median, pctile  # noqa: E402
from lib.gap10y_engine import DbCalendar, build_e2a_10y, build_e2b_10y, e2b_date_set, run_v1_v6  # noqa: E402
from lib import gap_common as gc  # noqa: E402
from lib import gap_engine as ge  # noqa: E402
from lib import pead_common as pc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000008" / "10-result"
PEAD_RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000007" / "10-result"
U6_CAP_LABEL = "gap"

Z_STAR_GRID = [round(-1.50 - 0.25 * i, 2) for i in range(27)]  # -1.50 .. -8.00, 0.25刻み・27点
N_ANN_TARGET = 215
ANNUALIZATION_CONST = 245

SIGMA_R_FIXED = 0.04  # DS-5b/DS-6bの事前固定σ_R


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def quarter_key(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7])
    q = (m - 1) // 3 + 1
    return f"{y}Q{q}"


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA query_only=ON")
    cal_json = load_calendar()
    cal = Calendar(cal_json)
    universe_json = load_universe()
    uidx = UniverseIndex(universe_json, U6_CAP_LABEL)

    # 9-4: 本EXPで使うユニバースコード = 全確定日にわたる gap(175cap) の和集合
    gap_codes: set[str] = set()
    for rec in universe_json["per_date"]:
        gap_codes |= set(rec["by_u6_cap"][U6_CAP_LABEL]["codes"])
    gap_codes = sorted(gap_codes)
    log(f"gap universe union codes: {len(gap_codes)}")

    log("DbCalendar構築中（全期間の日足をロード）...")
    dbcal = DbCalendar(conn, cal.T, gap_codes)
    log(f"  missing_bars_codes={len(dbcal.missing_bars_codes)}")

    # fins_summary（配当・E-3用。gap_codesに限定。SQLite変数上限(999)を避けチャンク分割）
    log("fins_summary読み込み中...")
    fins_records = []
    chunk = 500
    for i in range(0, len(gap_codes), chunk):
        sub = gap_codes[i:i + chunk]
        rows = conn.execute(
            f"SELECT json_blob FROM fins_summary WHERE code IN ({','.join('?'*len(sub))})", sub
        ).fetchall()
        fins_records.extend(json.loads(r[0]) for r in rows)
    log(f"  fins_summary件数（gapユニバース限定）={len(fins_records)}")

    # 9-2a/9-2b: 配当落ち日構成・検証（§3.1.1・決済サイクル分岐）
    basis_records, skipped_missing_fy = gc.construct_dividend_basis_records(fins_records)
    log(f"basis_records={len(basis_records)} skipped_missing_fy={skipped_missing_fy}")
    e2a_result = build_e2a_10y(dbcal, basis_records)
    e2b_by_month = build_e2b_10y(dbcal)
    e2b_dates = e2b_date_set(e2b_by_month)
    log(f"E2A pairs={len(e2a_result['e2a_map'])} E2B distinct dates={len(e2b_dates)} transition_band_count={e2a_result['transition_band_count']}")

    log("V-1〜V-6検証中（V-5含む。13.2是正）...")
    v_result = run_v1_v6(dbcal, e2a_result, gap_codes, fins_records=fins_records)
    log(
        f"V-pass: {v_result['all_v_pass']}  V1={v_result['V1_pass']} V2={v_result['V2_pass']} "
        f"V3={v_result['V3_pass']} V4={v_result['V4_pass']} V5={v_result['V5_pass']} V6={v_result['V6_pass']}"
    )

    e2a_set = set(e2a_result["e2a_map"].keys())

    # 10Y-COMMON §8.2.1（D-6是正・最優先）: E-1のOR条件実装修正を確認する。
    # AdjFactor!=1.0 の全行がExRTの値によらずE-1で除外されることを件数で検算する（gap_codesユニバースに限定）。
    log("D-6/E-1 OR条件検算中（§8.2.1）...")
    e1_adjfactor_ne1_total = 0
    e1_adjfactor_ne1_excluded = 0
    for code in gap_codes:
        for date_s, row in dbcal.bars_by_code.get(code, {}).items():
            af = row.get("AdjFactor")
            if af is not None and af != 1.0:
                e1_adjfactor_ne1_total += 1
                if gc.is_split_merger_row(row):
                    e1_adjfactor_ne1_excluded += 1
    e1_or_condition_check = {
        "e1_adjfactor_ne1_total": e1_adjfactor_ne1_total,
        "e1_adjfactor_ne1_excluded_by_is_split_merger_row": e1_adjfactor_ne1_excluded,
        "match": e1_adjfactor_ne1_total == e1_adjfactor_ne1_excluded,
        "methodology": (
            "gapユニバース和集合の全銘柄・全期間についてAdjFactor!=1.0の行を数え、"
            "is_split_merger_row()（spec§3.1のOR条件で修正済み）がその全行を除外判定することを検算した。"
            "一致すればExRTの意味（'2'/'3'）が未解決でもE-1の実害（未除外の混入）は排除されている（10Y-COMMON §8.2.1）。"
        ),
        "pass": e1_adjfactor_ne1_total == e1_adjfactor_ne1_excluded,
    }
    log(f"  e1_adjfactor_ne1_total={e1_adjfactor_ne1_total} excluded={e1_adjfactor_ne1_excluded} match={e1_or_condition_check['match']}")

    # 有効ギャップ位置・σ_gapキャッシュ（gap_engine.pyを再利用。DbCalendarはgc.Calendarと同インタフェース）
    log("有効ギャップ位置構築中（E-1修正反映後）...")
    valid_positions = ge.build_valid_positions(dbcal, e2a_set, e2b_dates)
    log("K_look分布計算中（9-5）...")
    k_dist = ge.compute_k_distribution(dbcal, valid_positions)
    log(f"  K_look max={k_dist['k_max']} coverage<=66={k_dist['k_le_66_coverage_rate']} gate_k_le_66_all={k_dist['gate_k_le_66_all']}")

    # 10Y-COMMON §8 D-6 / spec §13.3（K_look>66 停止規則。実際に処理を止める制御フローとして実装する）
    if not k_dist["gate_k_le_66_all"]:
        log("STOP: K_look>66が1件以上検出された（§3.2・§9-5・K-6）。上限66を延ばす独自判断は禁止（N-8）。処理を停止しSに差し戻す。")
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        stop_payload = {
            "stopped_at": "9-5_k_look_gate",
            "reason": "K_look>66 が1件以上存在する（spec §3.2・§9-5・K-6の無条件停止規則）",
            "k_look_distribution": k_dist,
            "e1_or_condition_check": e1_or_condition_check,
            "V_gates": v_result,
        }
        (RESULT_DIR / "feasibility.json").write_text(
            json.dumps(stop_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        log(f"saved (stop payload): {RESULT_DIR / 'feasibility.json'}")
        return 6

    log("gap値キャッシュ構築中...")
    gap_cache = ge.build_gap_value_cache(dbcal, valid_positions)

    # E-3用: DiscDate位置
    disc_dates_by_code = ge.build_disc_dates_by_code(fins_records)
    disc_positions_by_code = ge.build_disc_positions_by_code(dbcal, disc_dates_by_code)

    # 候補行の評価: i = T68_idx 〜 T_len（選定開始〜確認終了）。各日、その日の確定ユニバース（gap 175cap）に限定。
    i_start = cal_json["T68_idx"]
    i_end = cal_json["T_len"]
    log(f"候補行評価中: i={i_start}〜{i_end}（{i_end-i_start+1}日）...")

    all_rows = []
    codes_present_cache: dict[str, set[str]] = {}
    for i in range(i_start, i_end + 1):
        date_i = cal.at(i)
        universe_codes = uidx.codes_for(date_i)
        if not universe_codes:
            continue
        for code in universe_codes:
            if code not in dbcal.bars_by_code:
                continue
            if date_i not in dbcal.bars_by_code[code]:
                continue
            row = ge.evaluate_candidate_row(
                dbcal, code, i, valid_positions, gap_cache, e2a_set, e2b_dates, disc_positions_by_code
            )
            all_rows.append(row)
        if i % 200 == 0:
            log(f"  進捗 i={i}/{i_end} 累計行数={len(all_rows)}")
    log(f"候補行評価完了: 総行数={len(all_rows)}")

    # プールP: E-1〜E-6を満たし(is_candidate) かつ z<=-1.5
    pool = [r for r in all_rows if r["is_candidate"] and r["z"] is not None and r["z"] <= -1.5]
    log(f"プールP件数={len(pool)}")

    sel_lo, sel_hi = cal_json["selection_range"]
    conf_lo, conf_hi = cal_json["confirmation_range"]

    pool_sel = [r for r in pool if sel_lo <= r["date"] <= sel_hi]
    pool_conf = [r for r in pool if conf_lo <= r["date"] <= conf_hi]

    # D_eff（E-2Bによる全銘柄一律除外日を除いた対象日数）
    T = cal_json["T"]
    idx_of = {d: i + 1 for i, d in enumerate(T)}
    d_eff_sel = sum(1 for d in T if (idx_of[d] >= cal_json["T68_idx"] and idx_of[d] <= cal_json["s_idx"] - 1) and d not in e2b_dates)
    d_eff_conf = sum(1 for d in T if (idx_of[d] >= cal_json["s_idx"] and idx_of[d] <= cal_json["T_len"]) and d not in e2b_dates)
    log(f"D_eff(選定)={d_eff_sel} D_eff(確認)={d_eff_conf}")

    # z*較正: 選定期間のみ、グリッド27点それぞれの n(z*)・N_ann(z*)
    grid_results = []
    for zstar in Z_STAR_GRID:
        n_z = sum(1 for r in pool_sel if r["z"] <= zstar)
        n_ann = (n_z * ANNUALIZATION_CONST / d_eff_sel) if d_eff_sel else None
        grid_results.append({"z_star": zstar, "n_selection": n_z, "N_ann": n_ann, "abs_diff_from_target": (abs(n_ann - N_ANN_TARGET) if n_ann is not None else None)})

    # DS-2a: 選定期間でN_ann in [150,300]を満たす点が存在するか
    ds2a_candidates = [g for g in grid_results if g["N_ann"] is not None and 150 <= g["N_ann"] <= 300]
    ds2a_pass = len(ds2a_candidates) > 0

    # z*選定: |N_ann-215|最小、同値ならより極端側（より負）
    best = None
    for g in grid_results:
        if g["N_ann"] is None:
            continue
        if best is None:
            best = g
            continue
        if g["abs_diff_from_target"] < best["abs_diff_from_target"] - 1e-9:
            best = g
        elif abs(g["abs_diff_from_target"] - best["abs_diff_from_target"]) <= 1e-9 and g["z_star"] < best["z_star"]:
            best = g
    z_star = best["z_star"] if best else None
    log(f"z*較正結果: z*={z_star} (選定N_ann={best['N_ann'] if best else None})")

    # イベント集合E（確認期間）: プールのうち z<=z*
    event_conf = [r for r in pool_conf if z_star is not None and r["z"] <= z_star]
    n_ann_conf = (len(event_conf) * ANNUALIZATION_CONST / d_eff_conf) if d_eff_conf else None
    ds2b_pass = n_ann_conf is not None and 100 <= n_ann_conf <= 400

    # DS-1: 確認期間プール件数
    ds1_count = len(pool_conf)
    ds1_pass = ds1_count >= 6000

    # DS-3: 全確定日でgapユニバース>=150
    ds3_results = {rec["date"]: rec["by_u6_cap"][U6_CAP_LABEL]["final_count"] for rec in universe_json["per_date"]}
    ds3_pass = all(v >= 150 for v in ds3_results.values())

    # DS-4: 欠損営業日率（gapユニバースのみ）
    total_expected = 0
    total_actual = 0
    import bisect as _bisect
    for code in gap_codes:
        dates = sorted(dbcal.bars_by_code.get(code, {}).keys())
        if not dates:
            continue
        lo = _bisect.bisect_left(T, dates[0])
        hi = _bisect.bisect_right(T, dates[-1])
        expected = hi - lo
        actual = len(dates)
        total_expected += expected
        total_actual += actual
    ds4_missing_rate = ((total_expected - total_actual) / total_expected) if total_expected else None
    ds4_pass = ds4_missing_rate is not None and ds4_missing_rate <= 0.05

    # DS-5a/DS-5b: K_event（イベント集合Eの日数）とMDE_level
    by_date_event = Counter(r["date"] for r in event_conf)
    ds5a_days = len(by_date_event)
    ds5a_pass = ds5a_days >= 140
    k_d_avg = (sum(by_date_event.values()) / ds5a_days) if ds5a_days else None
    mde_level = None
    if k_d_avg and k_d_avg > 0 and ds5a_days > 0:
        mde_level = 1.645 * (SIGMA_R_FIXED / math.sqrt(k_d_avg)) / math.sqrt(ds5a_days)
    ds5b_pass = mde_level is not None and mde_level <= 0.0035

    # DS-6a/DS-6b: プールの日次ブロックのうち5件以上の日数K、MDE_IC
    by_date_pool_conf = Counter(r["date"] for r in pool_conf)
    days_ge5 = {d: c for d, c in by_date_pool_conf.items() if c >= 5}
    ds6a_days = len(days_ge5)
    ds6a_pass = ds6a_days >= 250
    n_d_avg = (sum(days_ge5.values()) / len(days_ge5)) if days_ge5 else None
    mde_ic = None
    if n_d_avg and n_d_avg > 1 and ds6a_days > 0:
        mde_ic = 1.645 / (math.sqrt(n_d_avg - 1) * math.sqrt(ds6a_days))
    ds6b_pass = mde_ic is not None and mde_ic <= 0.035

    # DS-7/DS-7b: 最大単日シェア・上位5日合計シェア（確認期間プール）
    max_day_count = max(by_date_pool_conf.values()) if by_date_pool_conf else 0
    ds7_share = (max_day_count / ds1_count) if ds1_count else None
    ds7_pass = ds7_share is not None and ds7_share <= 0.25
    top5_sum = sum(sorted(by_date_pool_conf.values(), reverse=True)[:5])
    ds7b_share = (top5_sum / ds1_count) if ds1_count else None
    ds7b_pass = ds7b_share is not None and ds7b_share <= 0.50

    # DS-8: イベントを含む暦四半期数（確認期間プール）
    quarters = {quarter_key(d) for d in by_date_pool_conf}
    ds8_count = len(quarters)
    ds8_pass = ds8_count >= 12

    all_ds_pass = (
        ds1_pass and ds2a_pass and ds2b_pass and ds3_pass and ds4_pass and
        ds5a_pass and ds5b_pass and ds6a_pass and ds6b_pass and ds7_pass and ds7b_pass and ds8_pass
    )

    ic_econ_at_fixed_sigma = 0.0045 / (1.605 * SIGMA_R_FIXED)

    ds_result = {
        "z_star_grid": grid_results,
        "z_star_selected": z_star,
        "D_eff_selection": d_eff_sel,
        "D_eff_confirmation": d_eff_conf,
        "DS-1_confirmation_pool_count": ds1_count,
        "DS-1_threshold": 6000,
        "DS-1_pass": ds1_pass,
        "DS-2a_pass": ds2a_pass,
        "DS-2a_qualifying_grid_points": [g["z_star"] for g in ds2a_candidates],
        "DS-2b_confirmation_N_ann": n_ann_conf,
        "DS-2b_range": [100, 400],
        "DS-2b_pass": ds2b_pass,
        "DS-3_universe_count_by_date": ds3_results,
        "DS-3_pass": ds3_pass,
        "DS-4_missing_business_day_rate": ds4_missing_rate,
        "DS-4_pass": ds4_pass,
        "DS-5a_K_event": ds5a_days,
        "DS-5a_threshold": 140,
        "DS-5a_pass": ds5a_pass,
        "DS-5b_MDE_level": mde_level,
        "DS-5b_threshold": 0.0035,
        "DS-5b_pass": ds5b_pass,
        "DS-6a_K": ds6a_days,
        "DS-6a_threshold": 250,
        "DS-6a_pass": ds6a_pass,
        "DS-6b_MDE_IC": mde_ic,
        "DS-6b_threshold": 0.035,
        "DS-6b_pass": ds6b_pass,
        "DS-7_max_single_day_share": ds7_share,
        "DS-7_threshold": 0.25,
        "DS-7_pass": ds7_pass,
        "DS-7b_top5_days_share": ds7b_share,
        "DS-7b_threshold": 0.50,
        "DS-7b_pass": ds7b_pass,
        "DS-8_distinct_calendar_quarters": ds8_count,
        "DS-8_threshold": 12,
        "DS-8_pass": ds8_pass,
        "all_pass": all_ds_pass,
        "sigma_R_fixed_const": SIGMA_R_FIXED,
        "IC_econ_at_fixed_sigma_R": ic_econ_at_fixed_sigma,
    }

    # E-3排反性チェック（9-7）: gap確認期間イベント日集合 と PEAD disc_dates/エントリー日の積集合
    pead_disc_dates_path = PEAD_RESULT_DIR / "pead_disc_dates.json"
    disjointness = {"checked": False}
    if pead_disc_dates_path.exists():
        pead_disc_dates = set(json.loads(pead_disc_dates_path.read_text(encoding="utf-8")))
        gap_event_dates = {r["date"] for r in event_conf}
        intersection = gap_event_dates & pead_disc_dates
        disjointness = {
            "checked": True,
            "gap_event_date_count": len(gap_event_dates),
            "pead_disc_date_count": len(pead_disc_dates),
            "intersection_count": len(intersection),
            "is_disjoint": len(intersection) == 0,
        }

    exclusion_counter = Counter()
    for r in all_rows:
        for ex in r["exclusions"]:
            exclusion_counter[ex] += 1

    feasibility = {
        "9-2a_dividend_basis": {"basis_records_count": len(basis_records), "skipped_missing_fy": skipped_missing_fy},
        "9-2b_e2a_e2b_and_validation": {
            "e2a_pairs": len(e2a_result["e2a_map"]),
            "e2b_distinct_dates": len(e2b_dates),
            "transition_band_count": e2a_result["transition_band_count"],
            "cycle_counter": e2a_result["cycle_counter"],
            "V_gates": v_result,
        },
        "8.2.1_e1_or_condition_check": e1_or_condition_check,
        "9-5_k_look_distribution": k_dist,
        "9-6_DS_gates": ds_result,
        "9-7_pead_disjointness": disjointness,
        "exclusion_counts_all_rows": dict(exclusion_counter),
        "total_candidate_rows_evaluated": len(all_rows),
        "pool_selection_count": len(pool_sel),
        "pool_confirmation_count": len(pool_conf),
        "gap_universe_union_codes_count": len(gap_codes),
    }

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "feasibility.json").write_text(json.dumps(feasibility, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"saved: {RESULT_DIR / 'feasibility.json'}")

    # プールP（is_candidate & z<=-1.5）をG1測定で再利用できるよう永続化する
    # （all_rowsの完全再計算を避けるため。件数が小さいため軽量）。
    pool_out = [
        {"code": r["code"], "date": r["date"], "g": r["g"], "sigma": r["sigma"], "z": r["z"], "S": r["S"]}
        for r in pool
    ]
    (RESULT_DIR / "gap_pool.json").write_text(json.dumps(pool_out, ensure_ascii=False), encoding="utf-8")
    log(f"saved: {RESULT_DIR / 'gap_pool.json'}（{len(pool_out)}件）")

    params = {
        "calendar": cal_json,
        "gap_ds_gate_result": ds_result,
        "z_star": z_star,
        "random_seed": 20260915,
        "u6_cap_applied": 175,
        "shared_universe_reference": "research/EXP-OBS000007/10-result/universe.json（10Y-COMMON §8 D-9で構築。本EXPはU6_cap=175で打ち切ったものを使用。271キャップはPEAD専用でありD-16はギャップEXPに適用しない）",
        "shared_data_layer_reference": {
            "raw_data_root": "data/raw/jq10y/",
            "d0_contract_range_probe": "data/raw/jq10y/d0_contract_range_probe.json",
            "d4_d8_common_tasks": "research/EXP-OBS000007/10-result/d4_d8_common_tasks.json（D-4/D-6はPEAD側で実行・本EXPと共有。10Y-COMMON §8.1/§8.2が正本）",
        },
        "e1_or_condition_check": e1_or_condition_check,
        "d6_note": (
            "D-6（ExRTの意味）はEXP-OBS000007側のd4_d8_common_tasks.jsonでPEAD向けに全件検算済み。"
            "本EXP（Gap）にとってのD-6の実害は、上記e1_or_condition_checkのmatch=Trueによって、"
            "ExRTの意味が未解決のままでも排除されていることを確認した（10Y-COMMON §8.2.1/§8.2.4）。"
        ),
    }
    (RESULT_DIR / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"saved: {RESULT_DIR / 'params.json'}")

    log(json.dumps({k: v for k, v in ds_result.items() if k.endswith("_pass")}, ensure_ascii=False, indent=2))

    if not v_result["all_v_pass"]:
        log("STOP: V-1〜V-6のいずれかが未達。実験を回さずSに差し戻す（K-6）。")
        return 3
    return 0 if all_ds_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
