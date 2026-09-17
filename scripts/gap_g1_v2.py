#!/usr/bin/env python3
"""EXP-OBS000006（ギャップ・10年データ版・spec第5版）G1（予測単位）測定。

spec: `research/EXP-OBS000006/01-spec.md` §6.1〜§6.3。

前提: `scripts/gap_feasibility_v2.py` が完了し、DSゲート（§6.0）が全合格していること
（`research/EXP-OBS000006/10-result/feasibility.json` の `can_proceed_to_G1: true`）。

**このスクリプトは `data/raw/` を一切書き込まない（読み取り専用）。**

判定語は書かない。すべて実測値・件数のみ。機械的な合否（○×）のみ出力する。

方法論上の注記（spec本文に明示が無いため、B実装チームが宣言する実装選択。C品質チームの検査対象）:
  - PEAD（EXP-OBS000005）の§4.3が明示する「約定不能イベントを除外した版／O(t1)で仮想的に
    含めた版の両方を出力し、主判定は除外版」という扱いを、本EXPのIC_bar・デシル・水準検定
    のすべてに一貫して適用する（GAP specの§6.1/§6.3にはこの二版化の明記が無いため、
    姉妹EXPの先例に倣った実装選択である）。除外対象はE-7(BUY_BLOCKED)・E-8。

乱数シード: 20260915（固定・permutation検定のみで使用）。

再現用コマンド:
    python3 scripts/gap_g1_v2.py
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gap_common as gc  # noqa: E402
from lib import gap_engine as ge  # noqa: E402
from lib import v2_common as v2  # noqa: E402
from lib.pead_common import buy_blocked, parse_ul_ll_flag  # noqa: E402

RESULT_DIR = v2.GAP_RESULT_DIR
SEED = 20260915
N_PERM = 10000
MIN_OBS_PER_DAY = 5
POOL_THRESHOLD = -1.5


def main() -> int:  # noqa: C901
    feas = json.loads((RESULT_DIR / "feasibility.json").read_text(encoding="utf-8"))
    if not feas.get("can_proceed_to_G1"):
        print("STOP: feasibility.json の can_proceed_to_G1 が false。§9が未完了・未達のためG1を実行しない。")
        return 2

    z_star = feas["9-6_z_star_calibration_and_ds_gates"]["z_star_frozen"]
    print(f"z_star_frozen={z_star}")

    codes = v2.load_candidate_codes_v2()
    cal = v2.CalendarV2(codes)
    N = len(cal.T)
    m = N // 2

    universe = json.loads((RESULT_DIR / "universe_v2.json").read_text(encoding="utf-8"))
    sel_universe = universe["selection_universe"]
    conf_universe = universe["confirmation_universe"]

    fins_all = gc.load_fins_summary_all()
    codes_set = set(codes)
    fins_candidates = [r for r in fins_all if r.get("Code") in codes_set]
    basis_records, _ = gc.construct_dividend_basis_records(fins_candidates)
    e2a = gc.build_e2a(cal, basis_records)
    e2b_dates = gc.build_e2b(cal)
    e2a_set = set(e2a["e2a_map"].keys())
    e2b_date_set = set(e2b_dates.values())

    valid_positions = ge.build_valid_positions(cal, e2a_set, e2b_date_set)
    gap_cache = ge.build_gap_value_cache(cal, valid_positions)
    disc_dates_by_code = ge.build_disc_dates_by_code(fins_candidates)
    disc_positions_by_code = ge.build_disc_positions_by_code(cal, disc_dates_by_code)

    I_SEL_START, I_SEL_END = 68, m - 11
    I_CONF_START, I_CONF_END = m + 1, N - 11
    sel_range = (cal.at(I_SEL_START), cal.at(I_SEL_END))
    conf_range = (cal.at(I_CONF_START), cal.at(I_CONF_END))
    print(f"selection_range={sel_range} confirmation_range={conf_range}")

    sel_rows = ge.build_period_rows(
        cal, sel_universe["codes"], I_SEL_START, I_SEL_END, valid_positions, gap_cache,
        e2a_set, e2b_date_set, disc_positions_by_code,
    )
    conf_rows = ge.build_period_rows(
        cal, conf_universe["codes"], I_CONF_START, I_CONF_END, valid_positions, gap_cache,
        e2a_set, e2b_date_set, disc_positions_by_code,
    )

    # ---------------- 価格付与（BUY_BLOCKED/E-8・R_5） ----------------
    def enrich(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            if not r["is_candidate"]:
                continue
            i = r["i"]
            t1 = cal.at(i + 1)
            t5 = cal.at(i + 5)
            row_t1 = cal.row(r["code"], t1) if t1 else None
            row_t5 = cal.row(r["code"], t5) if t5 else None
            if row_t1 is None:
                continue
            blocked, reason = buy_blocked(row_t1)
            e8 = False
            ll = parse_ul_ll_flag(row_t1.get("LL"))
            l_ = row_t1.get("L")
            o_ = row_t1.get("O")
            if ll is True and l_ is not None and o_ is not None and abs(float(o_) - float(l_)) <= 1e-6 * max(1.0, abs(float(l_))):
                e8 = True
            blocked_final = blocked or e8
            o1 = row_t1.get("O")
            c5 = row_t5.get("C") if row_t5 else None
            r5 = None
            if o1 is not None and c5 is not None and float(o1) > 0:
                r5 = float(c5) / float(o1) - 1.0
            out.append({**r, "t1": t1, "t5": t5, "buy_blocked": blocked_final, "block_reason": ("E8" if e8 else reason), "R_5": r5})
        return out

    sel_enriched = enrich(sel_rows)
    conf_enriched = enrich(conf_rows)
    print(f"selection_candidate_events={len(sel_enriched)} confirmation_candidate_events={len(conf_enriched)}")

    # ---------------- 市場プロキシ（同一窓のユニバース等加重平均R_5） ----------------
    def make_market_proxy(universe_codes: list[str]):
        cache: dict[int, float | None] = {}

        def proxy(i: int) -> float | None:
            if i in cache:
                return cache[i]
            t1 = cal.at(i + 1)
            t5 = cal.at(i + 5)
            rs = []
            for c in universe_codes:
                r1 = cal.row(c, t1) if t1 else None
                r5 = cal.row(c, t5) if t5 else None
                if r1 is None or r5 is None:
                    continue
                o1 = r1.get("O")
                c5v = r5.get("C")
                if o1 is None or c5v is None or float(o1) <= 0:
                    continue
                rs.append(float(c5v) / float(o1) - 1.0)
            val = (sum(rs) / len(rs)) if rs else None
            cache[i] = val
            return val
        return proxy

    conf_market_proxy = make_market_proxy(conf_universe["codes"])
    sel_market_proxy = make_market_proxy(sel_universe["codes"])

    for e in conf_enriched:
        mp = conf_market_proxy(e["i"])
        e["R_5_excess"] = (e["R_5"] - mp) if (e["R_5"] is not None and mp is not None) else None
    for e in sel_enriched:
        mp = sel_market_proxy(e["i"])
        e["R_5_excess"] = (e["R_5"] - mp) if (e["R_5"] is not None and mp is not None) else None

    # ---------------- Pool P (z<=-1.5) / Event set E (z<=z*) ----------------
    def pool_and_event(enriched: list[dict]):
        pool = [e for e in enriched if e["z"] <= POOL_THRESHOLD]
        event = [e for e in pool if e["z"] <= z_star]
        return pool, event

    conf_pool, conf_event = pool_and_event(conf_enriched)
    sel_pool, sel_event = pool_and_event(sel_enriched)
    print(f"confirmation: pool={len(conf_pool)} event={len(conf_event)}")
    print(f"selection: pool={len(sel_pool)} event={len(sel_event)}")

    # ---------------- N-6: K(pool day>=5) と DS-6 の一致確認 ----------------
    conf_pool_by_day_raw = Counter(e["date"] for e in conf_pool)
    K_raw = sum(1 for c in conf_pool_by_day_raw.values() if c >= MIN_OBS_PER_DAY)
    ds6_feas = feas["9-6_z_star_calibration_and_ds_gates"]["DS-6_confirmation_days_with_ge5_pool_obs_K"]["value"]
    k_matches_ds6 = (K_raw == ds6_feas)
    print(f"K(pool raw, >=5/day)={K_raw} DS-6(feasibility)={ds6_feas} matches={k_matches_ds6}")

    # ---------------- IC_bar（プールP・S vs R_5^excess） ----------------
    def compute_ic_bar(pool: list[dict], exclude_blocked: bool):
        by_day: dict[str, list[dict]] = defaultdict(list)
        for e in pool:
            by_day[e["date"]].append(e)
        qualifying_days = {d: es for d, es in by_day.items() if len(es) >= MIN_OBS_PER_DAY}

        day_rx: dict[str, list[float]] = {}
        day_ry: dict[str, list[float]] = {}
        day_ic: dict[str, float] = {}
        day_n: dict[str, int] = {}
        undefined_days = []

        for d, es in sorted(qualifying_days.items()):
            use = [e for e in es if not (exclude_blocked and e["buy_blocked"])]
            use = [e for e in use if e["R_5_excess"] is not None]
            if len(use) < 2:
                undefined_days.append({"date": d, "usable_n": len(use), "raw_n": len(es)})
                continue
            xs = [e["S"] for e in use]
            ys = [e["R_5_excess"] for e in use]
            ic = gc.spearman(xs, ys)
            if ic is None:
                undefined_days.append({"date": d, "usable_n": len(use), "raw_n": len(es)})
                continue
            day_rx[d] = xs
            day_ry[d] = ys
            day_ic[d] = ic
            day_n[d] = len(use)

        if not day_ic:
            return None
        ic_bar = sum(day_ic.values()) / len(day_ic)
        return {
            "ic_bar": ic_bar, "day_ic": day_ic, "day_n": day_n, "day_rx": day_rx, "day_ry": day_ry,
            "K_total_ge5_raw_days": len(qualifying_days), "K_usable_for_ic": len(day_ic),
            "days_excluded_ic_undefined": undefined_days,
        }

    conf_ic_primary = compute_ic_bar(conf_pool, exclude_blocked=True)
    conf_ic_virtual = compute_ic_bar(conf_pool, exclude_blocked=False)
    sel_ic_primary = compute_ic_bar(sel_pool, exclude_blocked=True)
    print(f"confirmation IC_bar(primary)={conf_ic_primary['ic_bar'] if conf_ic_primary else None} K_usable={conf_ic_primary['K_usable_for_ic'] if conf_ic_primary else None}")
    print(f"confirmation IC_bar(virtual)={conf_ic_virtual['ic_bar'] if conf_ic_virtual else None}")
    print(f"selection IC_bar(primary)={sel_ic_primary['ic_bar'] if sel_ic_primary else None}")

    perm_result = run_permutation_test(conf_ic_primary) if conf_ic_primary else None
    if perm_result:
        print(f"permutation(IC): obs={perm_result['ic_bar_obs']:.6f} p={perm_result['p_value']:.6f} null_sd={perm_result['null_sd']:.6f}")

    # ---------------- 水準検定 M_bar（イベント集合E。G1-3の統計量。p値は報告のみ） ----------------
    m_bar_result = compute_m_bar(conf_event, conf_universe["codes"], cal, exclude_blocked=True)
    if m_bar_result:
        print(f"confirmation M_bar(primary)={m_bar_result['m_bar']:.6f} K_event={m_bar_result['K_event']}")
    label_perm = run_label_permutation_test(conf_event, conf_universe["codes"], cal, exclude_blocked=True) if m_bar_result else None
    if label_perm:
        print(f"permutation(M_bar label): obs={label_perm['m_bar_obs']:.6f} p={label_perm['p_value']:.6f}")

    # ---------------- デシル分析（プールP・確認期間・同一期間内Sの昇順10分割） ----------------
    decile_result = compute_deciles(conf_pool, exclude_blocked=True)

    # ---------------- G1-6/G1-3: イベント集合Eのグロス平均R_5（選定・確認） ----------------
    def event_gross_mean(event: list[dict], exclude_blocked: bool):
        use = [e for e in event if not (exclude_blocked and e["buy_blocked"]) and e["R_5"] is not None]
        if not use:
            return None, 0
        return sum(e["R_5"] for e in use) / len(use), len(use)

    conf_event_gross_mean, conf_event_gross_n = event_gross_mean(conf_event, True)
    sel_event_gross_mean, sel_event_gross_n = event_gross_mean(sel_event, True)
    print(f"confirmation event gross R_5 mean={conf_event_gross_mean} n={conf_event_gross_n}")
    print(f"selection event gross R_5 mean={sel_event_gross_mean} n={sel_event_gross_n}")

    # ---------------- G1-7: 最大件数の1日を除いた再計算 ----------------
    conf_event_by_day = Counter(e["date"] for e in conf_event)
    max_day = conf_event_by_day.most_common(1)[0][0] if conf_event_by_day else None
    conf_pool_wo_maxday = [e for e in conf_pool if e["date"] != max_day]
    conf_event_wo_maxday = [e for e in conf_event if e["date"] != max_day]
    ic_wo_maxday = compute_ic_bar(conf_pool_wo_maxday, exclude_blocked=True)
    gross_wo_maxday, gross_wo_maxday_n = event_gross_mean(conf_event_wo_maxday, True)
    g1_7_pass = (
        ic_wo_maxday is not None and ic_wo_maxday["ic_bar"] > 0
        and gross_wo_maxday is not None and gross_wo_maxday > 0
    )
    print(f"G1-7: max_day={max_day} IC_bar_wo={ic_wo_maxday['ic_bar'] if ic_wo_maxday else None} "
          f"gross_wo={gross_wo_maxday} pass={g1_7_pass}")

    # ---------------- G1判定 ----------------
    g1_1_pass = (conf_ic_primary is not None and conf_ic_primary["ic_bar"] > 0
                 and perm_result is not None and perm_result["p_value"] < 0.05)
    g1_2_pass = sel_ic_primary is not None and sel_ic_primary["ic_bar"] > 0
    g1_3_pass = conf_event_gross_mean is not None and conf_event_gross_mean >= 0.0045
    g1_4_pass = m_bar_result is not None and m_bar_result["m_bar"] > 0
    rho_dec = decile_result["rho_dec"] if decile_result else None
    g1_5_pass = rho_dec is not None and rho_dec >= 0.30
    g1_6_pass = sel_event_gross_mean is not None and sel_event_gross_mean > 0

    g1_all_pass = g1_1_pass and g1_2_pass and g1_3_pass and g1_4_pass and g1_5_pass and g1_6_pass and g1_7_pass

    print(f"G1-1={g1_1_pass} G1-2={g1_2_pass} G1-3={g1_3_pass} G1-4={g1_4_pass} "
          f"G1-5={g1_5_pass}(rho={rho_dec}) G1-6={g1_6_pass} G1-7={g1_7_pass} ALL={g1_all_pass}")

    # ---------------- 出力 ----------------
    output = {
        "generated_from": "gap_g1_v2.py",
        "spec_reference": "research/EXP-OBS000006/01-spec.md（第5版）§6.1〜§6.3",
        "seed": SEED, "n_permutations": N_PERM, "z_star_frozen": z_star,
        "methodology_note_execution_feasibility_dual_version": (
            "PEAD(EXP-OBS000005)§4.3の先例に倣い、BUY_BLOCKED(E-7)/E-8で除外した'主判定版'と、"
            "O(t1)で仮想的に含めた'仮想版'の両方を計算した。主判定にはprimary(除外版)を用いる。"
            "GAP spec自体には二版化の明記がないため、B実装チームの実装選択として明示する。"
        ),
        "selection_range": list(sel_range), "confirmation_range": list(conf_range),
        "selection_universe_count": len(sel_universe["codes"]), "confirmation_universe_count": len(conf_universe["codes"]),
        "K_pool_raw_ge5_per_day_confirmation": K_raw, "K_matches_DS6": k_matches_ds6,
        "confirmation_pool_n": len(conf_pool), "confirmation_event_n": len(conf_event),
        "selection_pool_n": len(sel_pool), "selection_event_n": len(sel_event),
        "confirmation_IC_bar_primary": strip_heavy(conf_ic_primary),
        "confirmation_IC_bar_virtual": strip_heavy(conf_ic_virtual),
        "selection_IC_bar_primary": strip_heavy(sel_ic_primary),
        "permutation_test_IC": perm_summary(perm_result),
        "level_test_M_bar": m_bar_result,
        "level_test_label_permutation": label_perm_summary(label_perm),
        "power_disclosure_6_1_4": power_disclosure(conf_ic_primary, m_bar_result),
        "decile_analysis": decile_result,
        "event_gross_r5": {
            "confirmation_mean": conf_event_gross_mean, "confirmation_n": conf_event_gross_n,
            "selection_mean": sel_event_gross_mean, "selection_n": sel_event_gross_n,
        },
        "g1_7_max_day_exclusion": {
            "max_event_day": max_day, "max_day_event_count": conf_event_by_day.get(max_day) if max_day else None,
            "ic_bar_excl_max_day": ic_wo_maxday["ic_bar"] if ic_wo_maxday else None,
            "gross_r5_excl_max_day": gross_wo_maxday, "gross_r5_excl_max_day_n": gross_wo_maxday_n,
            "pass": g1_7_pass,
        },
        "g1_conditions": {
            "G1-1_confirmation_ic_bar_positive_significant": {"ic_bar": conf_ic_primary["ic_bar"] if conf_ic_primary else None,
                                                               "p_value": perm_result["p_value"] if perm_result else None, "pass": g1_1_pass},
            "G1-2_selection_ic_bar_positive": {"ic_bar": sel_ic_primary["ic_bar"] if sel_ic_primary else None, "pass": g1_2_pass},
            "G1-3_confirmation_event_gross_ge_0.45pct": {"value": conf_event_gross_mean, "threshold": 0.0045, "pass": g1_3_pass},
            "G1-4_confirmation_m_bar_positive": {"m_bar": m_bar_result["m_bar"] if m_bar_result else None, "pass": g1_4_pass},
            "G1-5_pool_decile_monotonicity_rho_dec_ge_0.30": {"rho_dec": rho_dec, "threshold": 0.30, "pass": g1_5_pass},
            "G1-6_selection_event_gross_positive": {"value": sel_event_gross_mean, "pass": g1_6_pass},
            "G1-7_single_day_dependency_removed": {"pass": g1_7_pass},
        },
        "g1_all_pass": g1_all_pass,
        "N-6_check": {"K_raw": K_raw, "DS-6_feasibility": ds6_feas, "matches": k_matches_ds6},
    }
    (RESULT_DIR / "prediction-unit.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"saved: {RESULT_DIR / 'prediction-unit.json'}")
    print(f"g1_all_pass={g1_all_pass}")
    return 0


def strip_heavy(ic_result: dict | None) -> dict | None:
    if ic_result is None:
        return None
    return {k: v for k, v in ic_result.items() if k not in ("day_rx", "day_ry")}


def perm_summary(perm_result: dict | None) -> dict | None:
    if perm_result is None:
        return None
    return {k: v for k, v in perm_result.items() if k != "null_distribution_sample"} | {
        "null_distribution_first_20": perm_result["null_distribution_sample"][:20]
    }


def label_perm_summary(lp: dict | None) -> dict | None:
    if lp is None:
        return None
    return {k: v for k, v in lp.items() if k != "null_distribution_sample"} | {
        "null_distribution_first_20": lp["null_distribution_sample"][:20]
    }


def _rank(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg_rank
        i = j + 1
    return r


def run_permutation_test(ic_primary: dict) -> dict:
    """spec §6.1.2-4: イベント日ブロック内permutation（Sをシャッフル）。ランク不変性で高速化。"""
    day_rx = ic_primary["day_rx"]
    day_ry = ic_primary["day_ry"]
    days = sorted(day_rx.keys())

    per_day_prep = {}
    for d in days:
        xs = day_rx[d]
        ys = day_ry[d]
        rx = _rank(xs)
        ry = _rank(ys)
        n = len(rx)
        mean_rx = sum(rx) / n
        mean_ry = sum(ry) / n
        cx = [v - mean_rx for v in rx]
        cy = [v - mean_ry for v in ry]
        sxx = sum(v * v for v in cx)
        syy = sum(v * v for v in cy)
        per_day_prep[d] = (cx, cy, sxx, syy, n)

    ic_bar_obs = ic_primary["ic_bar"]
    rng = random.Random(SEED)
    null_dist = []
    for _ in range(N_PERM):
        ic_sum = 0.0
        ic_count = 0
        for d in days:
            cx, cy, sxx, syy, n = per_day_prep[d]
            if sxx == 0 or syy == 0:
                continue
            idx = list(range(n))
            rng.shuffle(idx)
            cy_perm = [cy[k] for k in idx]
            sxy = sum(a * b for a, b in zip(cx, cy_perm))
            ic_sum += sxy / math.sqrt(sxx * syy)
            ic_count += 1
        if ic_count == 0:
            continue
        null_dist.append(ic_sum / ic_count)

    ge_count = sum(1 for v in null_dist if v >= ic_bar_obs)
    p_value = (1 + ge_count) / (1 + len(null_dist))
    mean_null = sum(null_dist) / len(null_dist)
    var_null = sum((v - mean_null) ** 2 for v in null_dist) / (len(null_dist) - 1)
    return {
        "ic_bar_obs": ic_bar_obs, "n_permutations_executed": len(null_dist), "n_permutations_requested": N_PERM,
        "seed": SEED, "ge_count": ge_count, "p_value": p_value,
        "null_mean": mean_null, "null_sd": math.sqrt(var_null), "null_distribution_sample": null_dist,
    }


def compute_m_bar(event: list[dict], universe_codes: list[str], cal, exclude_blocked: bool) -> dict | None:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in event:
        by_day[e["date"]].append(e)
    day_m: dict[str, float] = {}
    day_k: dict[str, int] = {}
    for d, es in sorted(by_day.items()):
        use = [e for e in es if not (exclude_blocked and e["buy_blocked"]) and e["R_5_excess"] is not None]
        if not use:
            continue
        day_m[d] = sum(e["R_5_excess"] for e in use) / len(use)
        day_k[d] = len(use)
    if not day_m:
        return None
    m_bar = sum(day_m.values()) / len(day_m)
    return {"m_bar": m_bar, "day_m": day_m, "day_k": day_k, "K_event": len(day_m)}


def run_label_permutation_test(event: list[dict], universe_codes: list[str], cal, exclude_blocked: bool) -> dict | None:
    """spec §6.1.3 step3: ラベルpermutation。各イベント日について、その日にt1〜t5が確保できる
    ユニバース銘柄からk_d銘柄を無作為抽出してM_barを再計算。10,000回・seed=20260915。
    p値は報告のみ（合否ゲートではない）。
    """
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in event:
        by_day[e["date"]].append(e)
    day_k_d: dict[str, int] = {}
    day_i: dict[str, int] = {}
    for d, es in by_day.items():
        use = [e for e in es if not (exclude_blocked and e["buy_blocked"]) and e["R_5_excess"] is not None]
        if not use:
            continue
        day_k_d[d] = len(use)
        day_i[d] = es[0]["i"]

    if not day_k_d:
        return None

    # 各日について、その日にt1〜t5が確保できるユニバース全銘柄のR_5^excessを事前計算
    day_universe_pool: dict[str, list[float]] = {}
    for d, i in day_i.items():
        t1 = cal.at(i + 1)
        t5 = cal.at(i + 5)
        vals = []
        rs = []
        for c in universe_codes:
            r1 = cal.row(c, t1) if t1 else None
            r5 = cal.row(c, t5) if t5 else None
            if r1 is None or r5 is None:
                continue
            o1 = r1.get("O")
            c5v = r5.get("C")
            if o1 is None or c5v is None or float(o1) <= 0:
                continue
            rs.append(float(c5v) / float(o1) - 1.0)
        if rs:
            mean_r = sum(rs) / len(rs)
            vals = [r - mean_r for r in rs]  # 市場超過（同日ユニバース平均基準）に相当する近似
        day_universe_pool[d] = vals

    m_d_obs = {}
    for d, k_d in day_k_d.items():
        use = [e for e in by_day[d] if not (exclude_blocked and e["buy_blocked"]) and e["R_5_excess"] is not None]
        m_d_obs[d] = sum(e["R_5_excess"] for e in use) / len(use)
    m_bar_obs = sum(m_d_obs.values()) / len(m_d_obs)

    rng = random.Random(SEED)
    null_dist = []
    days = sorted(day_k_d.keys())
    for _ in range(N_PERM):
        m_sum = 0.0
        m_count = 0
        for d in days:
            pool = day_universe_pool.get(d)
            k_d = day_k_d[d]
            if not pool or len(pool) < k_d:
                continue
            sample = rng.sample(pool, k_d)
            m_sum += sum(sample) / len(sample)
            m_count += 1
        if m_count == 0:
            continue
        null_dist.append(m_sum / m_count)

    ge_count = sum(1 for v in null_dist if v >= m_bar_obs)
    p_value = (1 + ge_count) / (1 + len(null_dist))
    return {
        "m_bar_obs": m_bar_obs, "n_permutations_executed": len(null_dist), "n_permutations_requested": N_PERM,
        "seed": SEED, "ge_count": ge_count, "p_value": p_value, "null_distribution_sample": null_dist,
        "note": "この p 値は G1 の合否ゲートではない（spec §6.1.3）。報告のみ。",
    }


def power_disclosure(ic_primary: dict | None, m_bar_result: dict | None) -> dict:
    out = {}
    if ic_primary is not None:
        K = ic_primary["K_usable_for_ic"]
        ns = list(ic_primary["day_n"].values())
        n_d_mean = sum(ns) / len(ns) if ns else None
        sd_approx = (1.0 / math.sqrt(n_d_mean - 1)) if (n_d_mean and n_d_mean > 1) else None
        se = (sd_approx / math.sqrt(K)) if (sd_approx and K) else None
        mde = 1.645 * se if se else None
        out["ic_bar"] = {"K": K, "n_d_mean": n_d_mean, "SD_approx": sd_approx, "SE": se, "MDE": mde}
    if m_bar_result is not None:
        K_event = m_bar_result["K_event"]
        ks = list(m_bar_result["day_k"].values())
        k_d_mean = sum(ks) / len(ks) if ks else None
        out["m_bar"] = {"K_event": K_event, "k_d_mean": k_d_mean}
    return out


def compute_deciles(pool: list[dict], exclude_blocked: bool) -> dict | None:
    """spec §6.3-1: プールPをSの昇順で10分割（同一期間内の単純ランク分割。トレーリングではない）。"""
    usable = [e for e in pool if not (exclude_blocked and e["buy_blocked"]) and e["R_5"] is not None and e["R_5_excess"] is not None]
    if not usable:
        return None
    usable_sorted = sorted(usable, key=lambda e: e["S"])
    n = len(usable_sorted)
    deciles_out = {}
    boundaries = [round(n * k / 10) for k in range(11)]
    for dec in range(1, 11):
        lo, hi = boundaries[dec - 1], boundaries[dec]
        group = usable_sorted[lo:hi]
        if not group:
            deciles_out[dec] = {"n": 0}
            continue
        r5s = sorted(e["R_5"] for e in group)
        excess = [e["R_5_excess"] for e in group]
        nn = len(r5s)
        mean_ = sum(r5s) / nn
        median_ = r5s[nn // 2] if nn % 2 else (r5s[nn // 2 - 1] + r5s[nn // 2]) / 2.0
        std_ = math.sqrt(sum((x - mean_) ** 2 for x in r5s) / (nn - 1)) if nn > 1 else None
        deciles_out[dec] = {
            "n": nn, "gross_mean": mean_, "gross_median": median_, "gross_std": std_,
            "market_excess_mean": sum(excess) / len(excess),
            "s_range": [group[0]["S"], group[-1]["S"]],
        }
    dec_nums = [d for d in range(1, 11) if deciles_out[d]["n"] > 0]
    dec_means = [deciles_out[d]["gross_mean"] for d in dec_nums]
    rho_dec = gc.spearman([float(x) for x in dec_nums], dec_means) if len(dec_nums) >= 2 else None
    return {"usable_pool_events": n, "deciles": deciles_out, "rho_dec": rho_dec}


if __name__ == "__main__":
    raise SystemExit(main())
