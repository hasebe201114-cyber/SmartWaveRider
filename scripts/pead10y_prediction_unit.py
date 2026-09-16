#!/usr/bin/env python3
"""EXP-OBS000007（PEAD・10年版）§6.1〜§6.3 G1（予測単位）測定。

前提: `pead10y_feasibility.py` が DS ゲート全合格していること（N-10）。
乱数シード: 20260915固定（permutation・SUEpctには乱数を使わないため実際に使うのはpermutationのみ）。

出力: `research/EXP-OBS000007/10-result/prediction-unit.json`
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jq10y_common import (  # noqa: E402
    DB_PATH, Calendar, UniverseIndex, load_calendar, load_universe, spearman, median, pctile,
    make_rng, build_all_disclosures_index, compute_raw_sue_for_code,
)
from lib import pead_common as pc  # noqa: E402

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000007" / "10-result"
U6_CAP_LABEL = "pead"
N_PERM = 10000
SLIPPAGE_BUY = 0.00075


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_all_bars(conn: sqlite3.Connection, codes: set[str]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    codes = list(codes)
    for i in range(0, len(codes), 500):
        sub = codes[i:i + 500]
        rows = conn.execute(
            f"SELECT code,date,o,c,ul,ll,h,l,vo,adjo,adjc FROM bars WHERE code IN ({','.join('?'*len(sub))})", sub
        ).fetchall()
        for code, date, o, c, ul, ll, h, l, vo, adjo, adjc in rows:
            out.setdefault(code, {})[date] = {"O": o, "C": c, "UL": ul, "LL": ll, "H": h, "L": l, "Vo": vo, "AdjO": adjo, "AdjC": adjc}
    return out


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA query_only=ON")
    cal_json = load_calendar()
    cal = Calendar(cal_json)
    universe_json = load_universe()
    uidx = UniverseIndex(universe_json, U6_CAP_LABEL)

    ds_result = json.loads((RESULT_DIR / "feasibility.json").read_text(encoding="utf-8"))["9-3_DS_gates"]
    if not ds_result["all_pass"]:
        log("STOP: DSゲート未達のためG1を実行しない（N-10）。")
        return 3

    log("SUEイベント再計算中...")
    rows = conn.execute("SELECT json_blob FROM fins_summary").fetchall()
    records = [json.loads(r[0]) for r in rows]
    by_code = build_all_disclosures_index(records)
    all_events = []
    for code, disclosures in by_code.items():
        all_events.extend(compute_raw_sue_for_code(disclosures))
    valid_events_all = [e for e in all_events if e["raw_sue"] is not None]
    log(f"有効SUEイベント数（全期間・全銘柄）={len(valid_events_all)}")

    # ユニバース所属フィルタ（governing date時点のPEAD 271cap）
    universe_events = [e for e in valid_events_all if e["code"] in uidx.codes_for(e["disc_date"])]
    log(f"ユニバース内イベント数={len(universe_events)}")

    codes_needed = {e["code"] for e in universe_events}
    log(f"必要銘柄数={len(codes_needed)}。bars読み込み中...")
    bars = load_all_bars(conn, codes_needed)

    T = cal.T
    idx_of = {d: i + 1 for i, d in enumerate(T)}

    def entry_and_forward(code: str, disc_date: str) -> dict:
        i0 = idx_of.get(disc_date)
        if i0 is None:
            return {"status": "disc_date_not_in_T"}
        i1 = i0 + 1
        t1 = cal.at(i1)
        if t1 is None:
            return {"status": "t1_unavailable"}
        i5 = i1 + 4
        t5 = cal.at(i5)
        if t5 is None:
            return {"status": "forward_window_unavailable"}
        row1 = bars.get(code, {}).get(t1)
        row5 = bars.get(code, {}).get(t5)
        if row1 is None or row5 is None:
            return {"status": "missing_bar_row"}
        vo1, o1, ul1, h1 = row1.get("Vo"), row1.get("O"), row1.get("UL"), row1.get("H")
        buy_blocked = False
        reason = "not_blocked"
        if vo1 is None or float(vo1) == 0.0:
            buy_blocked, reason = True, "vo_zero"
        elif o1 is None:
            buy_blocked, reason = True, "o_null"
        elif str(ul1) == "1" and h1 is not None and abs(float(o1) - float(h1)) <= 1e-6 * max(1.0, abs(float(h1))):
            buy_blocked, reason = True, "ul_stop_high_open"
        adjo1, adjc5 = row1.get("AdjO"), row5.get("AdjC")
        r5 = None
        if adjo1 is not None and adjc5 is not None and float(adjo1) != 0:
            r5 = float(adjc5) / float(adjo1) - 1.0
        return {
            "status": "ok", "t1": t1, "t5": t5, "buy_blocked": buy_blocked, "blocked_reason": reason,
            "r5_gross": r5, "va_rank_top175": None,
        }

    log("エントリー・前方リターン計算中...")
    enriched = []
    status_counter = Counter()
    for e in universe_events:
        fw = entry_and_forward(e["code"], e["disc_date"])
        status_counter[fw["status"]] += 1
        if fw["status"] != "ok" or fw.get("r5_gross") is None:
            continue
        enriched.append({**e, **fw})
    log(f"forward計算ステータス: {dict(status_counter)}  有効件数={len(enriched)}")

    # 除外/仮想包含版の分離
    excluded_blocked = [e for e in enriched if e["buy_blocked"]]
    included_virtual = enriched  # 仮想的に含めた版（buy_blockedでもO(t1)ベースR5は既に計算済み）
    excluded_version = [e for e in enriched if not e["buy_blocked"]]
    log(f"約定不能（除外対象）={len(excluded_blocked)} 除外後の主判定件数={len(excluded_version)}")

    sel_lo, sel_hi = cal_json["selection_range"]
    conf_lo, conf_hi = cal_json["confirmation_range"]

    def ic_bar_for(events: list[dict], lo: str, hi: str) -> dict:
        by_date: dict[str, list[dict]] = defaultdict(list)
        for e in events:
            if lo <= e["disc_date"] <= hi:
                by_date[e["disc_date"]].append(e)
        day_ics = []
        day_records = []
        for d, evs in by_date.items():
            if len(evs) < 5:
                continue
            sues = [x["raw_sue"] for x in evs]
            r5s = [x["r5_gross"] for x in evs]
            ic = spearman(sues, r5s)
            if ic is not None:
                day_ics.append(ic)
                day_records.append({"date": d, "n": len(evs), "ic": ic})
        ic_bar = (sum(day_ics) / len(day_ics)) if day_ics else None
        return {"ic_bar": ic_bar, "K": len(day_ics), "day_records": day_records, "by_date_raw": by_date}

    log("確認期間IC_bar計算中（除外版）...")
    conf_ic = ic_bar_for(excluded_version, conf_lo, conf_hi)
    log(f"  IC_bar={conf_ic['ic_bar']} K={conf_ic['K']}")

    log("確認期間IC_bar計算中（仮想包含版）...")
    conf_ic_virtual = ic_bar_for(included_virtual, conf_lo, conf_hi)

    log("選定期間IC_bar計算中...")
    sel_ic = ic_bar_for(excluded_version, sel_lo, sel_hi)

    # permutation検定（開示日ブロック内シャッフル。10000回・シード20260915）
    log("permutation検定実行中（10000回）...")
    rng = make_rng()
    by_date_conf = conf_ic["by_date_raw"]
    perm_days = [(d, evs) for d, evs in by_date_conf.items() if len(evs) >= 5]
    perm_obs = conf_ic["ic_bar"]
    perm_ics = []
    if perm_days and perm_obs is not None:
        base_sues_by_day = [[x["raw_sue"] for x in evs] for d, evs in perm_days]
        r5s_by_day = [[x["r5_gross"] for x in evs] for d, evs in perm_days]
        for _ in range(N_PERM):
            day_ics = []
            for sues, r5s in zip(base_sues_by_day, r5s_by_day):
                shuffled = sues[:]
                rng.shuffle(shuffled)
                ic = spearman(shuffled, r5s)
                if ic is not None:
                    day_ics.append(ic)
            if day_ics:
                perm_ics.append(sum(day_ics) / len(day_ics))
    ge_count = sum(1 for x in perm_ics if x >= perm_obs) if perm_obs is not None else None
    p_value = ((1 + ge_count) / (1 + len(perm_ics))) if perm_ics and ge_count is not None else None
    perm_std = None
    if len(perm_ics) > 1:
        _perm_mean = sum(perm_ics) / len(perm_ics)
        perm_std = (sum((x - _perm_mean) ** 2 for x in perm_ics) / (len(perm_ics) - 1)) ** 0.5
    log(f"  permutation p={p_value}  n_reps={len(perm_ics)}")

    g1_1_pass = (perm_obs is not None and perm_obs > 0 and p_value is not None and p_value < 0.05)
    g1_2_pass = (sel_ic["ic_bar"] is not None and sel_ic["ic_bar"] > 0)

    # SUEpct（拡大窓トレーリング百分位。selection start以降通算）
    log("SUEpct（デシル分析用トレーリング百分位）計算中...")
    all_sorted_by_date = sorted(universe_events, key=lambda e: (e["disc_date"], e.get("disc_time", "")))
    running_sue: list[float] = []
    suepct_map: dict[tuple, float] = {}
    insufficient_window_count = 0
    for e in all_sorted_by_date:
        n = len(running_sue)
        if n < 200:
            insufficient_window_count += 1
        else:
            s = sorted(running_sue)
            # spec §3.4「同値（タイ）は平均順位で処理する」（EXP-OBS000007 §14.2 是正指示）。
            # bisect_left単独（タイ集団の最小順位）ではなく、タイ集団の下端・上端の中点を rank とする。
            lo = bisect.bisect_left(s, e["raw_sue"])
            hi = bisect.bisect_right(s, e["raw_sue"])
            rank = (lo + hi) / 2.0  # 平均順位（タイ集団の中央）
            pct = rank / n
            suepct_map[(e["code"], e["disc_date"], e.get("disc_no"))] = pct
        running_sue.append(e["raw_sue"])

    for e in excluded_version:
        e["sue_pct"] = suepct_map.get((e["code"], e["disc_date"], e.get("disc_no")))

    decile_eligible_conf = [e for e in excluded_version if conf_lo <= e["disc_date"] <= conf_hi and e["sue_pct"] is not None]
    log(f"確認期間デシル分析対象={len(decile_eligible_conf)}（トレーリング窓不足除外={insufficient_window_count}件・全期間）")

    def decile_of(pct: float) -> int:
        d = int(pct * 10) + 1
        return min(10, max(1, d))

    for e in decile_eligible_conf:
        e["decile"] = decile_of(e["sue_pct"])

    decile_stats = {}
    for d in range(1, 11):
        vals = [e["r5_gross"] for e in decile_eligible_conf if e["decile"] == d]
        if vals:
            s = sorted(vals)
            decile_stats[d] = {"n": len(vals), "mean": sum(vals)/len(vals), "median": pctile(s, 0.5),
                                "stdev": (sum((v-sum(vals)/len(vals))**2 for v in vals)/(len(vals)-1))**0.5 if len(vals) > 1 else None}
        else:
            decile_stats[d] = {"n": 0}

    top_decile_vals = [e["r5_gross"] for e in decile_eligible_conf if e["decile"] == 10]
    g1_3_value = (sum(top_decile_vals) / len(top_decile_vals)) if top_decile_vals else None
    g1_3_pass = g1_3_value is not None and g1_3_value >= 0.0035

    # 市場超過リターン（同一窓t1のユニバース等加重平均R5）
    log("市場超過リターン計算中...")
    t1_windows = sorted({e["t1"] for e in decile_eligible_conf})
    market_avg_by_t1 = {}
    for t1 in t1_windows:
        i1 = idx_of[t1]
        t5 = cal.at(i1 + 4)
        if t5 is None:
            continue
        codes_univ = uidx.codes_for(t1)
        rs = []
        for c in codes_univ:
            r1 = bars.get(c, {}).get(t1)
            r5row = bars.get(c, {}).get(t5)
            if r1 is None or r5row is None:
                continue
            ao, ac = r1.get("AdjO"), r5row.get("AdjC")
            if ao and ac and float(ao) != 0:
                rs.append(float(ac)/float(ao) - 1.0)
        if rs:
            market_avg_by_t1[t1] = sum(rs) / len(rs)

    for e in decile_eligible_conf:
        m = market_avg_by_t1.get(e["t1"])
        e["r5_excess"] = (e["r5_gross"] - m) if m is not None else None

    top3_vals = [e["r5_excess"] for e in decile_eligible_conf if e["decile"] in (8, 9, 10) and e["r5_excess"] is not None]
    g1_4_value = (sum(top3_vals)/len(top3_vals)) if top3_vals else None
    g1_4_pass = g1_4_value is not None and g1_4_value > 0

    decile_means_excess = []
    for d in range(1, 11):
        vals = [e["r5_excess"] for e in decile_eligible_conf if e["decile"] == d and e["r5_excess"] is not None]
        decile_means_excess.append(sum(vals)/len(vals) if vals else None)
    valid_pairs = [(d+1, v) for d, v in enumerate(decile_means_excess) if v is not None]
    rho_dec = spearman([p[0] for p in valid_pairs], [p[1] for p in valid_pairs]) if len(valid_pairs) >= 2 else None
    g1_5_pass = rho_dec is not None and rho_dec >= 0.30

    # G1-6: 最大シーズン（暦四半期）を除いて再計算
    def quarter_key(d: str) -> str:
        y, m = int(d[:4]), int(d[5:7])
        return f"{y}Q{(m-1)//3+1}"
    q_counts = Counter(quarter_key(e["disc_date"]) for e in excluded_version if conf_lo <= e["disc_date"] <= conf_hi)
    max_q = q_counts.most_common(1)[0][0] if q_counts else None
    excl_no_maxq = [e for e in excluded_version if not (conf_lo <= e["disc_date"] <= conf_hi and quarter_key(e["disc_date"]) == max_q)]
    ic_no_maxq = ic_bar_for(excl_no_maxq, conf_lo, conf_hi)
    top_decile_no_maxq = [e["r5_gross"] for e in decile_eligible_conf if quarter_key(e["disc_date"]) != max_q and e["decile"] == 10]
    g1_6_ic_sign = ic_no_maxq["ic_bar"] is not None and ic_no_maxq["ic_bar"] > 0
    g1_6_decile_mean = (sum(top_decile_no_maxq)/len(top_decile_no_maxq)) if top_decile_no_maxq else None
    g1_6_pass = g1_6_ic_sign and (g1_6_decile_mean is not None and g1_6_decile_mean > 0)

    # G1-7: Va上位175銘柄に限定
    top175_events = [e for e in excluded_version if e["code"] in uidx.top175_codes_for(e["disc_date"])]
    ic_top175 = ic_bar_for(top175_events, conf_lo, conf_hi)
    top175_decile_events = [e for e in decile_eligible_conf if e["code"] in uidx.top175_codes_for(e["disc_date"])]
    top175_top_decile = [e["r5_gross"] for e in top175_decile_events if e["decile"] == 10]
    g1_7_decile_mean = (sum(top175_top_decile)/len(top175_top_decile)) if top175_top_decile else None
    g1_7_pass = (ic_top175["ic_bar"] is not None and ic_top175["ic_bar"] > 0) and (g1_7_decile_mean is not None and g1_7_decile_mean > 0)

    all_g1_pass = g1_1_pass and g1_2_pass and g1_3_pass and g1_4_pass and g1_5_pass and g1_6_pass and g1_7_pass

    r5_all_vals = [e["r5_gross"] for e in excluded_version if conf_lo <= e["disc_date"] <= conf_hi]
    sigma_r_measured = None
    if len(r5_all_vals) > 1:
        m = sum(r5_all_vals)/len(r5_all_vals)
        sigma_r_measured = (sum((v-m)**2 for v in r5_all_vals)/(len(r5_all_vals)-1))**0.5

    result = {
        "G1-1_confirmation_IC_bar": conf_ic["ic_bar"], "G1-1_K": conf_ic["K"],
        "G1-1_permutation_p_value": p_value, "G1-1_permutation_reps": len(perm_ics),
        "G1-1_permutation_std": perm_std, "G1-1_pass": g1_1_pass,
        "G1-1_virtual_included_IC_bar": conf_ic_virtual["ic_bar"],
        "G1-2_selection_IC_bar": sel_ic["ic_bar"], "G1-2_selection_K": sel_ic["K"], "G1-2_pass": g1_2_pass,
        "G1-3_top_decile_R5_gross_mean": g1_3_value, "G1-3_threshold": 0.0035, "G1-3_pass": g1_3_pass,
        "G1-4_top3_decile_market_excess_mean": g1_4_value, "G1-4_pass": g1_4_pass,
        "G1-5_rho_dec": rho_dec, "G1-5_pass": g1_5_pass,
        "G1-6_max_quarter_excluded": max_q, "G1-6_IC_bar_excl": ic_no_maxq["ic_bar"],
        "G1-6_top_decile_mean_excl": g1_6_decile_mean, "G1-6_pass": g1_6_pass,
        "G1-7_IC_bar_top175": ic_top175["ic_bar"], "G1-7_top_decile_mean_top175": g1_7_decile_mean,
        "G1-7_pass": g1_7_pass,
        "all_G1_pass": all_g1_pass,
        "decile_stats_gross": decile_stats,
        "n_d_avg_confirmation": (sum(len(v) for d, v in conf_ic["by_date_raw"].items() if len(v) >= 5) / conf_ic["K"]) if conf_ic["K"] else None,
        "sigma_R_measured_confirmation": sigma_r_measured,
        "R5_measured_stats": {"n": len(r5_all_vals), "mean": (sum(r5_all_vals)/len(r5_all_vals) if r5_all_vals else None), "stdev": sigma_r_measured},
        "buy_blocked_excluded_count": len(excluded_blocked),
        "status_counter_forward_calc": dict(status_counter),
        "total_universe_events": len(universe_events),
    }

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "prediction-unit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"saved: {RESULT_DIR / 'prediction-unit.json'}")
    log(json.dumps({k: v for k, v in result.items() if k.endswith("_pass")}, ensure_ascii=False, indent=2))

    return 0 if all_g1_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
