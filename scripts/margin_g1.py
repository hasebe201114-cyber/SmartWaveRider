#!/usr/bin/env python3
"""EXP-OBS000008（信用倍率単独の方向予測力検定）G1（予測単位）測定。

spec: `research/EXP-OBS000008/01-spec.md`（第3版）§6.1〜§6.3。

前提: `scripts/margin_feasibility.py` が完了し `research/EXP-OBS000008/10-result/feasibility.json`
の `can_proceed_to_G1: true` であること（DSゲート・9-9b・9-9c・9-10がすべて合格・Sの実測値と一致
済みであること）。

**このスクリプトは `data/raw/` を一切書き込まない（読み取り専用）。**

判定語は書かない。実測値と、spec §6.2の閾値に対する機械的な合否（true/false）のみ出力する。
解釈・採否の所見は一切書かない。

R_5計算式（spec §4.3・第2版）: `R_5 = AdjC(T[k+9]) / AdjO(T[k+4]) - 1`（k = idx_T(W[w])）。
BUY_BLOCKED/SELL_BLOCKED はG1には適用しない（spec §4.3・グロス統計量）。

乱数シード: 20260917（spec §6.1-4で固定・変更禁止）。permutation回数: 10,000。

方法論上の注記（spec本文に明示が無いため、B実装チームが宣言する実装選択。C品質チームの検査対象）:
  - §6.3-2の「市場超過リターン」の市場プロキシは、G2用のU-1〜U-8フィルタ済みユニバース
    （本タスクでは未構築・対象外）ではなく、候補554銘柄のうちその週のt_1/t_5に有効な
    AdjO/AdjC価格を持つ銘柄全件の等加重平均R_5とした。理由: spec全体で正式に定義された
    「ユニバース」概念は§5.2（G2専用のU-1〜U-8）のみであり、G1段階ではG2のユニバース構築を
    行わない（本タスクの指示どおりG2は対象外）。したがって「全銘柄」の最も単純で恣意性の
    無い解釈として、G1が既に読み込んでいる候補554銘柄プールをそのまま使用した。

再現用コマンド:
    python3 scripts/margin_g1.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gap_common as gc  # noqa: E402
from lib import margin_common as mc  # noqa: E402
from lib import v2_common as v2  # noqa: E402

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000008" / "10-result"
SEED = 20260917
N_PERM = 10000
MIN_EVENTS_PER_WEEK = 5
BUFFER_B = 4
ROUNDTRIP_COST_PRIMARY = 0.0035  # spec §8.1（片道脚あたり）。G1-3閾値は往復コスト0.35%×2脚分=0.70%
G1_3_THRESHOLD = 0.0070
G1_5_RHO_THRESHOLD = -0.30
K_EFF_THRESHOLD = 15


def main() -> int:  # noqa: C901
    feas = json.loads((RESULT_DIR / "feasibility.json").read_text(encoding="utf-8"))
    if not feas.get("can_proceed_to_G1"):
        print("STOP: feasibility.json の can_proceed_to_G1 が false。§9が未完了・未達のためG1を実行しない。")
        return 2

    codes = json.loads((RESULT_DIR / "candidate_codes_v3.json").read_text(encoding="utf-8"))["codes"]
    print(f"candidate_codes={len(codes)}")

    cal = v2.CalendarV2(codes)
    all_series = mc.build_all_series(codes)
    W = mc.build_weekly_calendar(all_series)
    by_date_index = mc.index_delta_m_by_date(all_series)

    N_w = len(W)
    m_w = N_w // 2
    sel_range_w = (1, m_w - BUFFER_B)
    conf_range_w = (m_w + 1, N_w - BUFFER_B)
    print(f"N_w={N_w} m_w={m_w} sel_range_w={sel_range_w} conf_range_w={conf_range_w}")

    # ---------------- 市場プロキシ（候補554銘柄・同一週t_1〜t_5の等加重平均R_5。AdjO/AdjCベース） ----------------
    mkt_cache: dict[int, float | None] = {}

    def market_proxy(k: int) -> float | None:
        if k in mkt_cache:
            return mkt_cache[k]
        t1 = cal.at(k + 4)
        t5 = cal.at(k + 9)
        rs = []
        if t1 and t5:
            for c in codes:
                r1 = cal.row(c, t1)
                r5 = cal.row(c, t5)
                if r1 is None or r5 is None:
                    continue
                o1 = r1.get("AdjO")
                c5 = r5.get("AdjC")
                if o1 is None or c5 is None or float(o1) <= 0:
                    continue
                rs.append(float(c5) / float(o1) - 1.0)
        val = (sum(rs) / len(rs)) if rs else None
        mkt_cache[k] = val
        return val

    # ---------------- イベント構築（選定・確認）: 有効ΔM_wを持つ(code,週)ペア全件にAdjベースR_5を付与 ----------------
    def build_events(lo_w: int, hi_w: int) -> tuple[list[dict], list[dict]]:
        events: list[dict] = []
        price_missing: list[dict] = []
        for w in range(lo_w, hi_w + 1):
            date = W[w - 1]
            k = cal.idx(date)
            if k is None:
                continue
            entries = by_date_index.get(date, {})
            for code, entry in entries.items():
                if not entry["delta_m_valid"]:
                    continue
                t1 = cal.at(k + 4)
                t5 = cal.at(k + 9)
                row1 = cal.row(code, t1) if t1 else None
                row5 = cal.row(code, t5) if t5 else None
                adjo1 = row1.get("AdjO") if row1 else None
                adjc5 = row5.get("AdjC") if row5 else None
                if adjo1 is None or adjc5 is None or float(adjo1) <= 0:
                    price_missing.append({"week": w, "week_date": date, "code": code, "t1": t1, "t5": t5})
                    continue
                r5 = float(adjc5) / float(adjo1) - 1.0
                mp = market_proxy(k)
                events.append({
                    "week": w, "week_date": date, "k": k, "code": code, "t1": t1, "t5": t5,
                    "delta_m": entry["delta_m"], "m_level": entry["m_level"],
                    "R_5": r5, "market_proxy_R_5": mp,
                    "R_5_excess": (r5 - mp) if mp is not None else None,
                })
        return events, price_missing

    sel_events, sel_price_missing = build_events(*sel_range_w)
    conf_events, conf_price_missing = build_events(*conf_range_w)
    print(f"selection_events={len(sel_events)} (price_missing={len(sel_price_missing)}) "
          f"confirmation_events={len(conf_events)} (price_missing={len(conf_price_missing)})")

    # ---------------- IC_w・IC_bar（週内クロスセクションSpearman順位相関） ----------------
    def compute_ic_bar(events: list[dict], w_range: tuple[int, int], x_key: str = "delta_m", y_key: str = "R_5") -> dict | None:
        by_week: dict[int, list[dict]] = defaultdict(list)
        for e in events:
            if e.get(x_key) is not None and e.get(y_key) is not None:
                by_week[e["week"]].append(e)
        week_ic: dict[int, float] = {}
        week_n: dict[int, int] = {}
        week_rx: dict[int, list[float]] = {}
        week_ry: dict[int, list[float]] = {}
        week_date_of: dict[int, str] = {}
        excluded_weeks: list[dict] = []
        for w in range(w_range[0], w_range[1] + 1):
            es = by_week.get(w, [])
            if len(es) < MIN_EVENTS_PER_WEEK:
                excluded_weeks.append({"week": w, "n": len(es), "reason": "n_lt_5"})
                continue
            xs = [e[x_key] for e in es]
            ys = [e[y_key] for e in es]
            ic = gc.spearman(xs, ys)
            if ic is None:
                excluded_weeks.append({"week": w, "n": len(es), "reason": "ic_undefined"})
                continue
            week_ic[w] = ic
            week_n[w] = len(es)
            week_rx[w] = xs
            week_ry[w] = ys
            week_date_of[w] = es[0]["week_date"]
        if not week_ic:
            return None
        ic_bar = sum(week_ic.values()) / len(week_ic)
        return {
            "ic_bar": ic_bar, "week_ic": week_ic, "week_n": week_n, "week_rx": week_rx, "week_ry": week_ry,
            "week_date_of": week_date_of, "K_weeks_used": len(week_ic),
            "excluded_weeks": excluded_weeks, "excluded_weeks_count": len(excluded_weeks),
        }

    conf_ic = compute_ic_bar(conf_events, conf_range_w)
    sel_ic = compute_ic_bar(sel_events, sel_range_w)
    print(f"confirmation IC_bar={conf_ic['ic_bar'] if conf_ic else None} K={conf_ic['K_weeks_used'] if conf_ic else None}")
    print(f"selection IC_bar={sel_ic['ic_bar'] if sel_ic else None} K={sel_ic['K_weeks_used'] if sel_ic else None}")

    ds2_k = feas["9-3_data_sufficiency_gate"]["DS-2_confirmation_weeks_with_ge5_valid_deltaM"]["value"]
    k_matches_ds2 = conf_ic is not None and conf_ic["K_weeks_used"] == ds2_k
    print(f"N-6 check: K(G1)={conf_ic['K_weeks_used'] if conf_ic else None} DS-2(feasibility)={ds2_k} matches={k_matches_ds2}")

    # ---------------- permutation検定（週ブロック内でΔM_wをシャッフル・10,000回・seed固定） ----------------
    perm_result = run_permutation_test(conf_ic) if conf_ic else None
    if perm_result:
        print(f"permutation: obs={perm_result['ic_bar_obs']:.6f} p={perm_result['p_value']:.6f} "
              f"n_perm={perm_result['n_permutations_executed']}")

    # ---------------- K_eff（Newey-West自動帯域幅） ----------------
    k_eff_result = compute_k_eff(conf_ic) if conf_ic else None
    if k_eff_result:
        print(f"K_eff: K={k_eff_result['K']} L={k_eff_result['L']} K_eff={k_eff_result['K_eff']:.4f}")

    # ---------------- デシル分析（確認期間・ΔM_w昇順の等件数10分割） ----------------
    decile_result = compute_deciles(conf_events)

    # ---------------- G1判定 ----------------
    g1_1_ic_pass = conf_ic is not None and conf_ic["ic_bar"] < 0
    g1_1_p_pass = perm_result is not None and perm_result["p_value"] < 0.05
    g1_1_pass = g1_1_ic_pass and g1_1_p_pass

    g1_1b_pass = k_eff_result is not None and k_eff_result["K_eff"] >= K_EFF_THRESHOLD

    g1_2_pass = sel_ic is not None and sel_ic["ic_bar"] < 0

    dec1 = decile_result["deciles"].get(1) if decile_result else None
    dec10 = decile_result["deciles"].get(10) if decile_result else None
    spread = (dec1["gross_mean"] - dec10["gross_mean"]) if (dec1 and dec10 and dec1["n"] > 0 and dec10["n"] > 0) else None
    g1_3_pass = spread is not None and spread >= G1_3_THRESHOLD

    dec10_excess = dec10["market_excess_mean"] if (dec10 and dec10["n"] > 0) else None
    dec1_excess = dec1["market_excess_mean"] if (dec1 and dec1["n"] > 0) else None
    g1_4_pass = dec10_excess is not None and dec1_excess is not None and dec10_excess < 0 and dec1_excess > 0

    rho_dec = decile_result["rho_dec"] if decile_result else None
    g1_5_pass = rho_dec is not None and rho_dec <= G1_5_RHO_THRESHOLD

    g1_all_pass = g1_1_pass and g1_1b_pass and g1_2_pass and g1_3_pass and g1_4_pass and g1_5_pass

    print(f"G1-1(ic<0 & p<0.05)={g1_1_pass} G1-1b(K_eff>=15)={g1_1b_pass} G1-2(sel sign)={g1_2_pass} "
          f"G1-3(spread>=0.70%)={g1_3_pass} G1-4(market excess sign)={g1_4_pass} "
          f"G1-5(rho<=-0.30)={g1_5_pass} ALL={g1_all_pass}")

    # ---------------- §6.3 記述統計（必須出力の一部。判定には使わない） ----------------
    threshold_analysis = compute_threshold_analysis(conf_events)
    week_concentration = compute_week_concentration(conf_events, conf_ic)
    autocorr_table = k_eff_result["rho_l_lag1_10"] if k_eff_result else None
    regime_breakdown = compute_regime_breakdown(conf_events)
    trading_hours_breakdown = compute_trading_hours_breakdown(conf_events)

    # SA-1: M_w（水準）を用いた同一分析（感度分析のみ・合否には使わない）
    conf_ic_m_level = compute_ic_bar(conf_events, conf_range_w, x_key="m_level", y_key="R_5")
    decile_result_m_level = compute_deciles(conf_events, sort_key="m_level")

    # SA-2: PIT安全マージン+0営業日版のIC_bar（感度分析のみ・合否には使わない）
    conf_events_pit0, conf_pit0_price_missing = build_events_pit0(cal, W, by_date_index, conf_range_w, codes, market_proxy)
    conf_ic_pit0 = compute_ic_bar(conf_events_pit0, conf_range_w)

    output = {
        "generated_from": "margin_g1.py",
        "spec_reference": "research/EXP-OBS000008/01-spec.md（第3版）§6.1〜§6.3",
        "seed": SEED, "n_permutations": N_PERM,
        "r5_formula": "R_5 = AdjC(T[k+9]) / AdjO(T[k+4]) - 1（k = idx_T(W[w])）",
        "methodology_note_market_proxy": (
            "§6.3-2の市場超過リターンの市場プロキシは、候補554銘柄のうち当該週のt_1/t_5に有効な"
            "AdjO/AdjC価格を持つ銘柄全件の等加重平均R_5とした（G2のU-1〜U-8フィルタ済みユニバースは"
            "本タスク範囲外のため未構築・不使用）。B実装チームの宣言する実装選択であり、C品質チームの"
            "検査対象。"
        ),
        "selection_week_range_1indexed": list(sel_range_w),
        "confirmation_week_range_1indexed": list(conf_range_w),
        "selection_events_n": len(sel_events), "selection_events_price_missing_n": len(sel_price_missing),
        "confirmation_events_n": len(conf_events), "confirmation_events_price_missing_n": len(conf_price_missing),
        "confirmation_IC_bar": strip_heavy(conf_ic),
        "selection_IC_bar": strip_heavy(sel_ic),
        "N-6_check_K_equals_DS2": {"K_G1": conf_ic["K_weeks_used"] if conf_ic else None, "DS-2_feasibility": ds2_k, "matches": k_matches_ds2},
        "permutation_test": perm_summary(perm_result),
        "k_eff": (k_eff_result and {k: v for k, v in k_eff_result.items() if k != "rho_l_lag1_10"}) or None,
        "ic_w_autocorrelation_table_lag1_10": autocorr_table,
        "decile_analysis": decile_result,
        "threshold_non_monotonicity_check": threshold_analysis,
        "week_concentration_check": week_concentration,
        "regime_breakdown_by_calendar_year": regime_breakdown,
        "trading_hours_breakdown_B6": trading_hours_breakdown,
        "sensitivity_SA1_M_level": {
            "IC_bar": strip_heavy(conf_ic_m_level),
            "decile_analysis": decile_result_m_level,
            "note": "SA-1。M_w（水準）を主指標ΔM_wの代わりに用いた感度分析。合否判定には使わない。",
        },
        "sensitivity_SA2_PIT_margin_plus0": {
            "IC_bar": strip_heavy(conf_ic_pit0),
            "events_n": len(conf_events_pit0), "price_missing_n": len(conf_pit0_price_missing),
            "note": (
                "SA-2。PIT安全マージン+0営業日版（エントリーをT[k+3]・確認済み最小ラグそのまま、"
                "決済はエントリーの5営業日後T[k+8]）のIC_bar。合否判定には使わない。"
            ),
        },
        "sector_neutral_returns": {
            "note": (
                "未実施。S33（業種区分）の銘柄別マッピングが本タスクの既存キャッシュに直接紐づく形で"
                "揃っておらず、新規のmaster取得（9-1で既に完了済みの候補集合再構成の範囲外）を要するため、"
                "本タスクの範囲（G1測定）では実施していない。判定には使わない記述統計のため、G1合否には"
                "影響しない。"
            ),
        },
        "g1_conditions": {
            "G1-1_confirmation_ic_bar_negative_significant": {
                "ic_bar": conf_ic["ic_bar"] if conf_ic else None,
                "p_value": perm_result["p_value"] if perm_result else None,
                "ic_negative": g1_1_ic_pass, "p_lt_0.05": g1_1_p_pass, "pass": g1_1_pass,
            },
            "G1-1b_k_eff_ge_15": {
                "K_eff": k_eff_result["K_eff"] if k_eff_result else None, "threshold_min": K_EFF_THRESHOLD, "pass": g1_1b_pass,
            },
            "G1-2_selection_ic_bar_negative": {
                "ic_bar": sel_ic["ic_bar"] if sel_ic else None, "pass": g1_2_pass,
            },
            "G1-3_decile_spread_ge_0.70pct": {
                "decile1_gross_mean": dec1["gross_mean"] if dec1 else None,
                "decile10_gross_mean": dec10["gross_mean"] if dec10 else None,
                "spread": spread, "threshold_min": G1_3_THRESHOLD, "pass": g1_3_pass,
            },
            "G1-4_market_excess_sign": {
                "decile1_market_excess_mean": dec1_excess, "decile10_market_excess_mean": dec10_excess, "pass": g1_4_pass,
            },
            "G1-5_decile_monotonicity_rho_dec_le_minus_0.30": {
                "rho_dec": rho_dec, "threshold_max": G1_5_RHO_THRESHOLD, "pass": g1_5_pass,
            },
        },
        "g1_all_pass": g1_all_pass,
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
    return {k: v for k, v in ic_result.items() if k not in ("week_rx", "week_ry")}


def perm_summary(perm_result: dict | None) -> dict | None:
    if perm_result is None:
        return None
    return {k: v for k, v in perm_result.items() if k != "null_distribution_sample"} | {
        "null_distribution_first_20": perm_result["null_distribution_sample"][:20]
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
        for kk in range(i, j + 1):
            r[order[kk]] = avg_rank
        i = j + 1
    return r


def run_permutation_test(ic_primary: dict) -> dict:
    """spec §6.1-4: 週内permutation。同一週ブロックの内部でのみΔM_w（x）をシャッフルし
    IC_barを再計算する。10,000回・seed=20260917固定。片側p値（観測値が帰無分布より負側に
    出た比率）= (1 + #{IC_bar_perm <= IC_bar_obs}) / (1 + 10,000)。
    """
    week_rx = ic_primary["week_rx"]
    week_ry = ic_primary["week_ry"]
    weeks = sorted(week_rx.keys())

    per_week_prep = {}
    for w in weeks:
        xs = week_rx[w]
        ys = week_ry[w]
        rx = _rank(xs)
        ry = _rank(ys)
        n = len(rx)
        mean_rx = sum(rx) / n
        mean_ry = sum(ry) / n
        cx = [v - mean_rx for v in rx]
        cy = [v - mean_ry for v in ry]
        sxx = sum(v * v for v in cx)
        syy = sum(v * v for v in cy)
        per_week_prep[w] = (cx, cy, sxx, syy, n)

    ic_bar_obs = ic_primary["ic_bar"]
    rng = random.Random(SEED)
    null_dist = []
    for _ in range(N_PERM):
        ic_sum = 0.0
        ic_count = 0
        for w in weeks:
            cx, cy, sxx, syy, n = per_week_prep[w]
            if sxx == 0 or syy == 0:
                continue
            idx = list(range(n))
            rng.shuffle(idx)
            cx_perm = [cx[kk] for kk in idx]  # spec: ΔM_w（x）を週内でシャッフル
            sxy = sum(a * b for a, b in zip(cx_perm, cy))
            ic_sum += sxy / math.sqrt(sxx * syy)
            ic_count += 1
        if ic_count == 0:
            continue
        null_dist.append(ic_sum / ic_count)

    le_count = sum(1 for v in null_dist if v <= ic_bar_obs)
    p_value = (1 + le_count) / (1 + len(null_dist))
    mean_null = sum(null_dist) / len(null_dist)
    var_null = sum((v - mean_null) ** 2 for v in null_dist) / (len(null_dist) - 1)
    return {
        "ic_bar_obs": ic_bar_obs, "n_permutations_executed": len(null_dist), "n_permutations_requested": N_PERM,
        "seed": SEED, "le_count": le_count, "p_value": p_value,
        "null_mean": mean_null, "null_sd": math.sqrt(var_null), "null_distribution_sample": null_dist,
    }


def compute_k_eff(ic_primary: dict) -> dict:
    """spec §6.1-5: K_eff = K / (1 + 2*Σ_{l=1}^{L}(1-l/L)*ρ_l)、L = floor(4*(K/100)^(2/9))。"""
    weeks = sorted(ic_primary["week_ic"].keys())
    ic_series = [ic_primary["week_ic"][w] for w in weeks]
    K = len(ic_series)
    L = math.floor(4 * (K / 100) ** (2 / 9)) if K > 0 else 0
    mean_ic = sum(ic_series) / K
    denom_gamma0 = sum((v - mean_ic) ** 2 for v in ic_series)

    def rho(l: int) -> float:
        if denom_gamma0 == 0:
            return 0.0
        num = sum((ic_series[t] - mean_ic) * (ic_series[t - l] - mean_ic) for t in range(l, K))
        return num / denom_gamma0

    rho_l = {l: rho(l) for l in range(1, L + 1)} if L > 0 else {}
    rho_l_lag1_10 = {l: rho(l) for l in range(1, 11)} if K > 10 else {l: rho(l) for l in range(1, K)}
    denom = 1 + 2 * sum((1 - l / L) * rho_l[l] for l in range(1, L + 1)) if L > 0 else 1.0
    k_eff = K / denom if denom != 0 else None
    return {"K": K, "L": L, "rho_l": rho_l, "denom": denom, "K_eff": k_eff, "rho_l_lag1_10": rho_l_lag1_10}


def compute_deciles(events: list[dict], sort_key: str = "delta_m") -> dict | None:
    """spec §6.3-1: 確認期間の観測をΔM_w（またはSA-1でM_w）の昇順で10等件数分割する。
    デシル1=最小・デシル10=最大。
    """
    usable = [e for e in events if e.get(sort_key) is not None and e.get("R_5") is not None]
    if not usable:
        return None
    usable_sorted = sorted(usable, key=lambda e: e[sort_key])
    n = len(usable_sorted)
    boundaries = [round(n * k / 10) for k in range(11)]
    deciles_out: dict[int, dict] = {}
    for dec in range(1, 11):
        lo, hi = boundaries[dec - 1], boundaries[dec]
        group = usable_sorted[lo:hi]
        if not group:
            deciles_out[dec] = {"n": 0}
            continue
        r5s = sorted(e["R_5"] for e in group)
        excess = [e["R_5_excess"] for e in group if e.get("R_5_excess") is not None]
        nn = len(r5s)
        mean_ = sum(r5s) / nn
        median_ = r5s[nn // 2] if nn % 2 else (r5s[nn // 2 - 1] + r5s[nn // 2]) / 2.0
        std_ = math.sqrt(sum((x - mean_) ** 2 for x in r5s) / (nn - 1)) if nn > 1 else None
        deciles_out[dec] = {
            "n": nn, "gross_mean": mean_, "gross_median": median_, "gross_std": std_,
            "market_excess_mean": (sum(excess) / len(excess)) if excess else None,
            "market_excess_n": len(excess),
            f"{sort_key}_range": [group[0][sort_key], group[-1][sort_key]],
        }
    dec_nums = [d for d in range(1, 11) if deciles_out[d]["n"] > 0]
    dec_means = [deciles_out[d]["gross_mean"] for d in dec_nums]
    rho_dec = gc.spearman([float(x) for x in dec_nums], dec_means) if len(dec_nums) >= 2 else None
    return {"sort_key": sort_key, "usable_events": n, "deciles": deciles_out, "rho_dec": rho_dec}


def compute_threshold_analysis(events: list[dict]) -> dict:
    """spec §6.3-4: 上位/下位5%・10%・20%・30%の各閾値での平均リターン。"""
    usable = [e for e in events if e.get("delta_m") is not None and e.get("R_5") is not None]
    if not usable:
        return {}
    usable_sorted = sorted(usable, key=lambda e: e["delta_m"])
    n = len(usable_sorted)
    out = {}
    for pct in (0.05, 0.10, 0.20, 0.30):
        cnt = max(1, round(n * pct))
        bottom = usable_sorted[:cnt]
        top = usable_sorted[-cnt:]
        out[f"pct_{int(pct*100)}"] = {
            "n_per_side": cnt,
            "bottom_mean_R_5": sum(e["R_5"] for e in bottom) / len(bottom),
            "top_mean_R_5": sum(e["R_5"] for e in top) / len(top),
        }
    return out


def compute_week_concentration(events: list[dict], ic_result: dict | None) -> dict:
    """spec §6.3-5（OBS000029診断）: 1週あたり有効銘柄数の度数分布・上位3週の寄与率。"""
    by_week: dict[int, list[dict]] = defaultdict(list)
    for e in events:
        by_week[e["week"]].append(e)
    counts = {w: len(es) for w, es in by_week.items()}
    freq_dist = Counter(counts.values())
    week_sum_r5 = {w: sum(e["R_5"] for e in es if e["R_5"] is not None) for w, es in by_week.items()}
    total_r5 = sum(week_sum_r5.values())
    top3 = sorted(week_sum_r5.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
    top3_sum = sum(v for _, v in top3)
    top3_contribution_rate = (top3_sum / total_r5) if total_r5 != 0 else None
    return {
        "per_week_event_count_frequency_distribution": {str(k): v for k, v in sorted(freq_dist.items())},
        "total_R_5_sum": total_r5,
        "top3_weeks_by_abs_contribution": [{"week": w, "week_R_5_sum": v} for w, v in top3],
        "top3_weeks_contribution_rate_of_total": top3_contribution_rate,
        "K_weeks_used_for_ic": ic_result["K_weeks_used"] if ic_result else None,
    }


def compute_regime_breakdown(events: list[dict]) -> dict:
    """spec §6.3-8: 暦年別分解。"""
    by_year: dict[int, list[dict]] = defaultdict(list)
    for e in events:
        if e["R_5"] is None:
            continue
        year = int(e["week_date"][:4])
        by_year[year].append(e)
    out = {}
    for year, es in sorted(by_year.items()):
        r5s = [e["R_5"] for e in es]
        n = len(r5s)
        mean_ = sum(r5s) / n
        out[str(year)] = {"n": n, "gross_mean_R_5": mean_}
    return out


def compute_trading_hours_breakdown(events: list[dict]) -> dict:
    """spec §7 C項・B-6: 取引時間変更（2024-11-05大引け15:00→15:30）前後別の記述統計。合否には使わない。"""
    cutoff = "2024-11-05"
    before = [e for e in events if e["week_date"] < cutoff and e["R_5"] is not None]
    after = [e for e in events if e["week_date"] >= cutoff and e["R_5"] is not None]

    def stats(es: list[dict]) -> dict:
        if not es:
            return {"n": 0}
        r5s = [e["R_5"] for e in es]
        n = len(r5s)
        mean_ = sum(r5s) / n
        return {"n": n, "gross_mean_R_5": mean_}

    return {"before_2024-11-05": stats(before), "on_or_after_2024-11-05": stats(after)}


def build_events_pit0(cal, W, by_date_index, w_range, codes, market_proxy):
    """SA-2: PIT安全マージン+0営業日版。エントリーt_1'=T[k+3]（公表可能日そのもの）・
    決済t_5'=T[k+8]（エントリーの5営業日後、主版と同じホライズン長を維持）。感度分析のみ。
    """
    events = []
    price_missing = []
    for w in range(w_range[0], w_range[1] + 1):
        date = W[w - 1]
        k = cal.idx(date)
        if k is None:
            continue
        entries = by_date_index.get(date, {})
        for code, entry in entries.items():
            if not entry["delta_m_valid"]:
                continue
            t1 = cal.at(k + 3)
            t5 = cal.at(k + 8)
            row1 = cal.row(code, t1) if t1 else None
            row5 = cal.row(code, t5) if t5 else None
            adjo1 = row1.get("AdjO") if row1 else None
            adjc5 = row5.get("AdjC") if row5 else None
            if adjo1 is None or adjc5 is None or float(adjo1) <= 0:
                price_missing.append({"week": w, "week_date": date, "code": code, "t1": t1, "t5": t5})
                continue
            r5 = float(adjc5) / float(adjo1) - 1.0
            events.append({
                "week": w, "week_date": date, "k": k, "code": code, "t1": t1, "t5": t5,
                "delta_m": entry["delta_m"], "m_level": entry["m_level"], "R_5": r5,
            })
    return events, price_missing


if __name__ == "__main__":
    raise SystemExit(main())
