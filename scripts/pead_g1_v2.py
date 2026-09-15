#!/usr/bin/env python3
"""EXP-OBS000005（PEAD・10年データ版・spec第2版）G1（予測単位）測定。

spec: `research/EXP-OBS000005/01-spec.md` §6.1〜§6.3・§2.5。

前提: `scripts/pead_feasibility_v2.py` が完了し、DSゲート（§6.0）が全合格していること
（`research/EXP-OBS000005/10-result/feasibility.json` の `can_proceed_to_G1: true`）。

**このスクリプトは `data/raw/` を一切書き込まない（読み取り専用）。**

判定語は書かない。すべて実測値・件数のみ。機械的な合否（○×）のみ出力する。

乱数シード: 20260915（固定・permutation検定のみで使用）。

再現用コマンド:
    python3 scripts/pead_g1_v2.py
"""

from __future__ import annotations

import bisect
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gap_common as gc  # noqa: E402
from lib import v2_common as v2  # noqa: E402
from lib.pead_common import buy_blocked, load_fins_summary, build_all_disclosures_index, compute_raw_sue_for_code  # noqa: E402

RESULT_DIR = v2.PEAD_RESULT_DIR
SEED = 20260915
N_PERM = 10000
MIN_EVENTS_PER_DAY = 5
MIN_HISTORY_FOR_DECILE = 200
ENTRY_SLIPPAGE = 0.00075


def main() -> int:  # noqa: C901
    feas = json.loads((RESULT_DIR / "feasibility.json").read_text(encoding="utf-8"))
    if not feas.get("can_proceed_to_G1"):
        print("STOP: feasibility.json の can_proceed_to_G1 が false。§9が未完了・未達のためG1を実行しない。")
        return 2

    codes = v2.load_candidate_codes_v2()
    cal = v2.CalendarV2(codes)
    N = len(cal.T)
    m = N // 2

    universe = json.loads((RESULT_DIR / "universe_v2.json").read_text(encoding="utf-8"))
    sel_universe = universe["selection_universe"]
    conf_universe = universe["confirmation_universe"]
    sel_codes_set = set(sel_universe["codes"])
    conf_codes_set = set(conf_universe["codes"])

    sel_range = (cal.at(61), cal.at(m - 16))
    conf_range = (cal.at(m + 1), cal.at(N - 16))
    print(f"selection_range={sel_range} confirmation_range={conf_range}")

    # ---------------- 全有効SUEイベント（候補554銘柄・全期間。デシルのトレーリング母集団に使う） ----------------
    fins_records = load_fins_summary(codes_filter=set(codes))
    by_code = build_all_disclosures_index(fins_records)
    all_events: list[dict] = []
    for code, disclosures in by_code.items():
        all_events.extend(compute_raw_sue_for_code(disclosures))
    valid_events = [e for e in all_events if e["raw_sue"] is not None]
    valid_events.sort(key=lambda e: (e["disc_date"], e["code"]))
    print(f"valid_events_total={len(valid_events)}")

    # spec §3.4: RawSUEの厳密なゼロ件数の比率を必ず出力する
    exact_zero_count = sum(1 for e in valid_events if e["raw_sue"] == 0.0)
    raw_sue_exact_zero_stats = {
        "valid_events_total": len(valid_events),
        "exact_zero_count": exact_zero_count,
        "exact_zero_ratio": exact_zero_count / len(valid_events) if valid_events else None,
    }
    print(f"raw_sue_exact_zero_ratio={raw_sue_exact_zero_stats['exact_zero_ratio']}")

    # ---------------- 期間内イベントの抽出＋価格データの付与 ----------------
    def build_period_events(codes_set: set[str], date_range: tuple[str, str]) -> list[dict]:
        lo, hi = date_range
        out = []
        n_no_t_index = 0
        n_missing_entry_row = 0
        n_missing_exit_price = 0
        blocked_reason_counter: Counter = Counter()
        for e in valid_events:
            if e["code"] not in codes_set:
                continue
            d = e["disc_date"]
            if not (lo <= d <= hi):
                continue
            i = cal.idx(d)
            if i is None:
                n_no_t_index += 1
                continue
            t1 = cal.at(i + 1)
            t5 = cal.at(i + 5)
            row_t1 = cal.row(e["code"], t1) if t1 else None
            row_t5 = cal.row(e["code"], t5) if t5 else None
            if row_t1 is None:
                n_missing_entry_row += 1
                continue
            blocked, reason = buy_blocked(row_t1)
            blocked_reason_counter[reason] += 1
            o1 = row_t1.get("O")
            c5 = row_t5.get("C") if row_t5 else None
            if o1 is None or c5 is None or float(o1) <= 0:
                n_missing_exit_price += 1
                r5 = None
            else:
                r5 = float(c5) / float(o1) - 1.0
            out.append(
                {
                    "code": e["code"], "disc_date": d, "raw_sue": e["raw_sue"],
                    "i": i, "t1": t1, "t5": t5, "buy_blocked": blocked, "block_reason": reason,
                    "R_5": r5,
                }
            )
        diag = {
            "no_t_index_count": n_no_t_index, "missing_entry_row_count": n_missing_entry_row,
            "missing_exit_price_count": n_missing_exit_price,
            "buy_blocked_reason_counts": dict(blocked_reason_counter),
        }
        return out, diag

    sel_events, sel_diag = build_period_events(sel_codes_set, sel_range)
    conf_events, conf_diag = build_period_events(conf_codes_set, conf_range)
    print(f"selection_period_events={len(sel_events)} diag={sel_diag}")
    print(f"confirmation_period_events={len(conf_events)} diag={conf_diag}")

    # ---------------- N-6: K(§6.1-2) と DS-2/C7-1 の一致確認（raw valid event の日次カウント） ----------------
    conf_by_day_raw = Counter(e["disc_date"] for e in conf_events)
    K_raw = sum(1 for c in conf_by_day_raw.values() if c >= MIN_EVENTS_PER_DAY)
    ds2_from_feasibility = feas["9-5_data_sufficiency_gate"]["DS-2_days_with_ge5_events"]["value"]
    k_matches_ds2 = (K_raw == ds2_from_feasibility)
    print(f"K(raw, >=5events/day)={K_raw} DS-2(feasibility)={ds2_from_feasibility} matches={k_matches_ds2}")
    if not k_matches_ds2:
        print("STOP (K-6/N-6): G1のKとDS-2/C7-1が食い違う。Sに差し戻す。")
        write_stop(RESULT_DIR, "N-6: KとDS-2/C7-1が食い違う", K_raw, ds2_from_feasibility)
        return 2

    # ---------------- IC_bar（確認期間・除外版=主判定／仮想含む版） ----------------
    def compute_ic_bar(events: list[dict], require_min5_raw: bool, exclude_blocked: bool):
        by_day: dict[str, list[dict]] = defaultdict(list)
        for e in events:
            by_day[e["disc_date"]].append(e)
        qualifying_days = {d: evs for d, evs in by_day.items() if len(evs) >= MIN_EVENTS_PER_DAY} if require_min5_raw else by_day

        day_rx: dict[str, list[float]] = {}
        day_ry: dict[str, list[float]] = {}
        day_ic: dict[str, float] = {}
        day_n: dict[str, int] = {}
        excluded_events_for_price = 0
        days_with_lt2_usable = []
        days_with_undefined_ic = []

        for d, evs in sorted(qualifying_days.items()):
            use = [e for e in evs if not (exclude_blocked and e["buy_blocked"])]
            use = [e for e in use if e["R_5"] is not None]
            excluded_events_for_price += sum(1 for e in evs if e["R_5"] is None)
            if len(use) < 2:
                days_with_lt2_usable.append({"date": d, "usable_n": len(use), "raw_n": len(evs)})
                continue
            xs = [e["raw_sue"] for e in use]
            ys = [e["R_5"] for e in use]
            ic = gc.spearman(xs, ys)
            if ic is None:
                # usable_n>=2 だが Spearman が定義できない（RawSUE または R_5 が全件同値で分散ゼロ）。
                # 典型例: その日のイベント全件のRawSUEがちょうど0.0（会社予想が直前予想と完全一致）。
                reason = "zero_variance_rawsue" if len(set(xs)) == 1 else ("zero_variance_r5" if len(set(ys)) == 1 else "undefined")
                days_with_undefined_ic.append({"date": d, "usable_n": len(use), "raw_n": len(evs), "reason": reason})
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
            "days_excluded_lt2_usable_events": days_with_lt2_usable,
            "days_excluded_ic_undefined_zero_variance": days_with_undefined_ic,
            "events_excluded_missing_price": excluded_events_for_price,
        }

    conf_ic_primary = compute_ic_bar(conf_events, require_min5_raw=True, exclude_blocked=True)
    conf_ic_virtual = compute_ic_bar(conf_events, require_min5_raw=True, exclude_blocked=False)
    sel_ic_primary = compute_ic_bar(sel_events, require_min5_raw=True, exclude_blocked=True)

    print(f"confirmation IC_bar(primary/excluded)={conf_ic_primary['ic_bar'] if conf_ic_primary else None} "
          f"K_usable={conf_ic_primary['K_usable_for_ic'] if conf_ic_primary else None}")
    print(f"confirmation IC_bar(virtual/included)={conf_ic_virtual['ic_bar'] if conf_ic_virtual else None}")
    print(f"selection IC_bar(primary/excluded)={sel_ic_primary['ic_bar'] if sel_ic_primary else None}")

    # ---------------- permutation検定（確認期間・primary版のみ。§6.1-4） ----------------
    perm_result = None
    if conf_ic_primary is not None:
        perm_result = run_permutation_test(conf_ic_primary)
        print(f"permutation: obs_IC_bar={perm_result['ic_bar_obs']:.6f} p_value={perm_result['p_value']:.6f} "
              f"null_sd={perm_result['null_sd']:.6f}")

    # ---------------- デシル分析（確認期間・primary版母集団。トレーリングSUEpct・全候補の全期間履歴） ----------------
    decile_result = compute_deciles(valid_events, conf_events, conf_universe, cal, exclude_blocked=True)

    # ---------------- G1判定 ----------------
    g1_1_pass = (conf_ic_primary is not None and conf_ic_primary["ic_bar"] > 0
                 and perm_result is not None and perm_result["p_value"] < 0.05)
    g1_2_pass = sel_ic_primary is not None and sel_ic_primary["ic_bar"] > 0
    decile10 = decile_result["deciles"].get(10) if decile_result else None
    g1_3_pass = decile10 is not None and decile10["gross_mean"] is not None and decile10["gross_mean"] >= 0.0035
    top3 = decile_result["top3_deciles_market_excess_mean"] if decile_result else None
    g1_4_pass = top3 is not None and top3 > 0
    rho_dec = decile_result["rho_dec"] if decile_result else None
    g1_5_pass = rho_dec is not None and rho_dec >= 0.30

    g1_all_pass = g1_1_pass and g1_2_pass and g1_3_pass and g1_4_pass and g1_5_pass

    print(f"G1-1={g1_1_pass} G1-2={g1_2_pass} G1-3={g1_3_pass}(decile10={decile10['gross_mean'] if decile10 else None}) "
          f"G1-4={g1_4_pass}(top3_excess={top3}) G1-5={g1_5_pass}(rho_dec={rho_dec}) ALL={g1_all_pass}")

    # ---------------- B案（銘柄分割）: G1合格時のみ追加確認として実施。合否には使わない ----------------
    b_plan_result = None
    if g1_all_pass:
        b_plan_result = compute_b_plan(valid_events, cal, sel_codes_set | conf_codes_set, sel_range, conf_range, m, N)
        print(f"B案(銘柄分割) odd_IC={b_plan_result['odd_ic_bar']} even_IC={b_plan_result['even_ic_bar']} "
              f"sign_match={b_plan_result['sign_match']}")

    # ---------------- 出力 ----------------
    output = {
        "generated_from": "pead_g1_v2.py",
        "spec_reference": "research/EXP-OBS000005/01-spec.md（第2版）§6.1〜§6.3",
        "seed": SEED, "n_permutations": N_PERM,
        "selection_range": list(sel_range), "confirmation_range": list(conf_range),
        "selection_universe_count": len(sel_universe["codes"]), "confirmation_universe_count": len(conf_universe["codes"]),
        "K_raw_ge5_events_per_day_confirmation": K_raw,
        "K_matches_DS2_C7_1": k_matches_ds2,
        "raw_sue_exact_zero_stats_3_4": raw_sue_exact_zero_stats,
        "selection_period_events": len(sel_events), "confirmation_period_events": len(conf_events),
        "selection_diagnostics": sel_diag, "confirmation_diagnostics": conf_diag,
        "confirmation_IC_bar_primary_excluded_blocked": strip_heavy(conf_ic_primary),
        "confirmation_IC_bar_virtual_included_blocked": strip_heavy(conf_ic_virtual),
        "selection_IC_bar_primary_excluded_blocked": strip_heavy(sel_ic_primary),
        "permutation_test": perm_result_summary(perm_result),
        "power_disclosure_6_1_1": power_disclosure(conf_ic_primary),
        "decile_analysis": decile_result,
        "g1_conditions": {
            "G1-1_confirmation_ic_bar_positive_and_significant": {
                "ic_bar": conf_ic_primary["ic_bar"] if conf_ic_primary else None,
                "p_value": perm_result["p_value"] if perm_result else None,
                "pass": g1_1_pass,
            },
            "G1-2_selection_ic_bar_positive": {
                "ic_bar": sel_ic_primary["ic_bar"] if sel_ic_primary else None, "pass": g1_2_pass,
            },
            "G1-3_confirmation_decile10_gross_ge_0.35pct": {
                "decile10_gross_mean": decile10["gross_mean"] if decile10 else None,
                "threshold": 0.0035, "pass": g1_3_pass,
            },
            "G1-4_confirmation_top3decile_market_excess_positive": {
                "top3_market_excess_mean": top3, "pass": g1_4_pass,
            },
            "G1-5_decile_monotonicity_rho_dec_ge_0.30": {
                "rho_dec": rho_dec, "threshold": 0.30, "pass": g1_5_pass,
            },
        },
        "g1_all_pass": g1_all_pass,
        "b_plan_supplementary_stock_split_check": b_plan_result,
        "N-6_check": {"K_raw": K_raw, "DS-2_feasibility": ds2_from_feasibility, "matches": k_matches_ds2},
    }
    (RESULT_DIR / "prediction-unit.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"saved: {RESULT_DIR / 'prediction-unit.json'}")
    print(f"g1_all_pass={g1_all_pass}")
    return 0


def strip_heavy(ic_result: dict | None) -> dict | None:
    """day_rx/day_ryは巨大になりうるため出力からは外し、要約のみ残す。"""
    if ic_result is None:
        return None
    out = {k: v for k, v in ic_result.items() if k not in ("day_rx", "day_ry")}
    return out


def perm_result_summary(perm_result: dict | None) -> dict | None:
    if perm_result is None:
        return None
    return {k: v for k, v in perm_result.items() if k != "null_distribution_sample"} | {
        "null_distribution_first_20": perm_result["null_distribution_sample"][:20]
    }


def run_permutation_test(ic_primary: dict) -> dict:
    """spec §6.1-4: 開示日ブロック内permutation。ランクの相関構造を保ったまま高速化する。

    Spearman相関はランクのみに依存するため、日次のRawSUE/R_5の値をシャッフルして再ランク付け
    するのと、あらかじめ計算したランク列の対応をシャッフルするのは数学的に同値である
    （タイの平均順位処理も多重集合が変わらないため保存される）。ここでは後者を用いて
    10,000回×日数のSpearman再計算を高速化する。観測値自体は素直にSpearmanで計算する。
    """
    day_rx = ic_primary["day_rx"]
    day_ry = ic_primary["day_ry"]
    days = sorted(day_rx.keys())

    # 日ごとにランク・中心化済みランク・sxx/syyを事前計算
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
            corr = sxy / math.sqrt(sxx * syy)
            ic_sum += corr
            ic_count += 1
        if ic_count == 0:
            continue
        null_dist.append(ic_sum / ic_count)

    ge_count = sum(1 for v in null_dist if v >= ic_bar_obs)
    p_value = (1 + ge_count) / (1 + len(null_dist))
    mean_null = sum(null_dist) / len(null_dist)
    var_null = sum((v - mean_null) ** 2 for v in null_dist) / (len(null_dist) - 1)
    return {
        "ic_bar_obs": ic_bar_obs, "n_permutations_executed": len(null_dist),
        "n_permutations_requested": N_PERM, "seed": SEED,
        "ge_count": ge_count, "p_value": p_value,
        "null_mean": mean_null, "null_sd": math.sqrt(var_null),
        "null_distribution_sample": null_dist,
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


def power_disclosure(ic_primary: dict | None) -> dict:
    """spec §6.1.1: 検出力の事前開示（実測値の出力のみ。解釈はしない）。"""
    if ic_primary is None:
        return {}
    K = ic_primary["K_usable_for_ic"]
    ns = list(ic_primary["day_n"].values())
    n_d_mean = sum(ns) / len(ns) if ns else None
    sd_ic_d_approx = (1.0 / math.sqrt(n_d_mean - 1)) if (n_d_mean and n_d_mean > 1) else None
    se_ic_bar = (sd_ic_d_approx / math.sqrt(K)) if (sd_ic_d_approx and K) else None
    mde = 1.645 * se_ic_bar if se_ic_bar else None
    return {
        "K": K, "n_d_mean_block_size": n_d_mean,
        "SD_IC_d_approx_1_over_sqrt_nd_minus_1": sd_ic_d_approx,
        "SE_IC_bar": se_ic_bar, "MDE_IC_bar_1.645_times_SE": mde,
    }


def compute_deciles(valid_events: list[dict], conf_events: list[dict], conf_universe: dict, cal, exclude_blocked: bool) -> dict | None:
    """spec §3.4・§6.3: トレーリングSUEpct（先読みなし・拡大窓）で確認期間イベントをデシル分割する。

    母集団: 候補554銘柄の全有効RawSUEイベント（全期間）。DiscDateがtより厳密に前の全イベント。
    参照イベント数が200件未満ならデシル分析から除外する（IC計算からは除外しない。ここではデシル専用）。
    """
    # 全有効イベントのRawSUEを時系列順にソート済み（呼び出し側で保証）。逐次的にソート済みリストへ挿入し、
    # bisectで「tより厳密に前」の分位を求める。
    history_sorted: list[float] = []
    percentile_by_key: dict[tuple[str, str], float] = {}
    history_size_by_key: dict[tuple[str, str], int] = {}
    for e in valid_events:
        key = (e["code"], e["disc_date"])
        n_hist = len(history_sorted)
        if n_hist > 0:
            pos = bisect.bisect_right(history_sorted, e["raw_sue"])
            pct = pos / n_hist
        else:
            pct = None
        percentile_by_key[key] = pct
        history_size_by_key[key] = n_hist
        bisect.insort(history_sorted, e["raw_sue"])

    usable = [e for e in conf_events if not (exclude_blocked and e["buy_blocked"]) and e["R_5"] is not None]
    excluded_lt200 = 0
    decile_events: dict[int, list[dict]] = defaultdict(list)
    for e in usable:
        key = (e["code"], e["disc_date"])
        n_hist = history_size_by_key.get(key)
        pct = percentile_by_key.get(key)
        if n_hist is None or n_hist < MIN_HISTORY_FOR_DECILE or pct is None:
            excluded_lt200 += 1
            continue
        decile = min(10, max(1, math.ceil(pct * 10) if pct > 0 else 1))
        decile_events[decile].append(e)

    if not decile_events:
        return None

    # 市場プロキシ: 同一カレンダー窓(t1..t5)のユニバース等加重平均R_5。窓(=i)ごとにキャッシュ。
    universe_codes = conf_universe["codes"]
    market_cache: dict[int, float | None] = {}

    def market_proxy_for_i(i: int) -> float | None:
        if i in market_cache:
            return market_cache[i]
        t1 = cal.at(i + 1)
        t5 = cal.at(i + 5)
        rs = []
        for c in universe_codes:
            r1 = cal.row(c, t1) if t1 else None
            r5 = cal.row(c, t5) if t5 else None
            if r1 is None or r5 is None:
                continue
            o1 = r1.get("O")
            c5 = r5.get("C")
            if o1 is None or c5 is None or float(o1) <= 0:
                continue
            rs.append(float(c5) / float(o1) - 1.0)
        val = (sum(rs) / len(rs)) if rs else None
        market_cache[i] = val
        return val

    deciles_out = {}
    for dec in range(1, 11):
        evs = decile_events.get(dec, [])
        if not evs:
            deciles_out[dec] = {"n": 0, "gross_mean": None, "gross_median": None, "gross_std": None,
                                 "market_excess_mean": None}
            continue
        r5s = sorted(e["R_5"] for e in evs)
        n = len(r5s)
        mean_ = sum(r5s) / n
        median_ = r5s[n // 2] if n % 2 else (r5s[n // 2 - 1] + r5s[n // 2]) / 2.0
        std_ = math.sqrt(sum((x - mean_) ** 2 for x in r5s) / (n - 1)) if n > 1 else None
        excess_list = []
        for e in evs:
            mp = market_proxy_for_i(e["i"])
            if mp is not None:
                excess_list.append(e["R_5"] - mp)
        excess_mean = (sum(excess_list) / len(excess_list)) if excess_list else None
        deciles_out[dec] = {
            "n": n, "gross_mean": mean_, "gross_median": median_, "gross_std": std_,
            "market_excess_mean": excess_mean, "market_excess_n": len(excess_list),
        }

    # G1-4: 上位3デシル（8〜10）の市場超過平均
    top3_evs = decile_events.get(8, []) + decile_events.get(9, []) + decile_events.get(10, [])
    top3_excess = []
    for e in top3_evs:
        mp = market_proxy_for_i(e["i"])
        if mp is not None:
            top3_excess.append(e["R_5"] - mp)
    top3_mean = (sum(top3_excess) / len(top3_excess)) if top3_excess else None

    # G1-5: デシル番号とデシル平均R_5のSpearman順位相関
    dec_nums = [d for d in range(1, 11) if deciles_out[d]["n"] > 0]
    dec_means = [deciles_out[d]["gross_mean"] for d in dec_nums]
    rho_dec = gc.spearman([float(x) for x in dec_nums], dec_means) if len(dec_nums) >= 2 else None

    return {
        "methodology": (
            "SUEpct = (候補554銘柄の全有効RawSUEイベントのうち、DiscDateがtより厳密に前のものの中で"
            "RawSUE(t)以下である割合)。拡大窓・先読みなし。参照イベント数200件未満はデシル分析からのみ除外。"
            f"デシル対象母集団は確認期間の{'除外版（BUY_BLOCKED除去）' if exclude_blocked else '仮想含む版'}。"
        ),
        "usable_confirmation_events": len(usable),
        "excluded_insufficient_history_lt200": excluded_lt200,
        "deciles": deciles_out,
        "top3_deciles_market_excess_mean": top3_mean,
        "top3_deciles_n": len(top3_evs),
        "rho_dec": rho_dec,
    }


def compute_b_plan(valid_events, cal, all_universe_codes, sel_range, conf_range, m, N) -> dict:
    """spec §2.5: B案（銘柄分割）。全期間（T[61]〜T[N-16]）で奇数/偶数群のIC_barを算出し符号一致を見る。
    合否判定には使わない。追加情報としてのみ出力する。"""
    odd_codes = {c for c in all_universe_codes if int(c) % 2 == 1}
    even_codes = {c for c in all_universe_codes if int(c) % 2 == 0}
    full_lo, full_hi = cal.at(61), cal.at(N - 16)

    def events_for(codes_set):
        out = []
        for e in valid_events:
            if e["code"] not in codes_set:
                continue
            d = e["disc_date"]
            if not (full_lo <= d <= full_hi):
                continue
            i = cal.idx(d)
            if i is None:
                continue
            t1 = cal.at(i + 1)
            t5 = cal.at(i + 5)
            row1 = cal.row(e["code"], t1) if t1 else None
            row5 = cal.row(e["code"], t5) if t5 else None
            if row1 is None or row5 is None:
                continue
            o1 = row1.get("O")
            c5 = row5.get("C")
            if o1 is None or c5 is None or float(o1) <= 0:
                continue
            out.append({"disc_date": d, "raw_sue": e["raw_sue"], "R_5": float(c5) / float(o1) - 1.0})
        return out

    def ic_bar_simple(evs):
        by_day = defaultdict(list)
        for e in evs:
            by_day[e["disc_date"]].append(e)
        ics = []
        for d, es in by_day.items():
            if len(es) < MIN_EVENTS_PER_DAY:
                continue
            ic = gc.spearman([e["raw_sue"] for e in es], [e["R_5"] for e in es])
            if ic is not None:
                ics.append(ic)
        return (sum(ics) / len(ics)) if ics else None

    odd_ic = ic_bar_simple(events_for(odd_codes))
    even_ic = ic_bar_simple(events_for(even_codes))
    sign_match = (odd_ic is not None and even_ic is not None and (odd_ic > 0) == (even_ic > 0))
    return {"full_range": [full_lo, full_hi], "odd_ic_bar": odd_ic, "even_ic_bar": even_ic, "sign_match": sign_match}


def write_stop(result_dir: Path, reason: str, k_raw, ds2) -> None:
    (result_dir / "prediction-unit.json").write_text(
        json.dumps({"stop_reason": reason, "K_raw": k_raw, "DS-2_feasibility": ds2, "g1_all_pass": None},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
