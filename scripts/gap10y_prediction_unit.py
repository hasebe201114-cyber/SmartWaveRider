#!/usr/bin/env python3
"""EXP-OBS000006（ギャップ・10年版）§6.1〜§6.3 G1（予測単位）測定。

前提: `gap10y_feasibility.py` が DS ゲート（DS-1〜DS-8）・V-1〜V-6 全合格していること（N-11・K-6）。
`gap_pool.json`（候補プールP。is_candidate & z<=-1.5）を再利用する。

出力: `research/EXP-OBS000006/10-result/prediction-unit.json`
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jq10y_common import DB_PATH, Calendar, UniverseIndex, load_calendar, load_universe, spearman, median, make_rng  # noqa: E402

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000006" / "10-result"
U6_CAP_LABEL = "gap"
N_PERM = 10000


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_bars_for(conn: sqlite3.Connection, codes: set[str]) -> dict:
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

    feas = json.loads((RESULT_DIR / "feasibility.json").read_text(encoding="utf-8"))
    ds = feas["9-6_DS_gates"]
    v = feas["9-2b_e2a_e2b_and_validation"]["V_gates"]
    if not v["all_v_pass"]:
        log("STOP: V-1〜V-6未達（K-6）。")
        return 3
    if not ds["all_pass"]:
        log("STOP: DSゲート未達のためG1を実行しない（N-11）。")
        return 3
    z_star = ds["z_star_selected"]

    pool = json.loads((RESULT_DIR / "gap_pool.json").read_text(encoding="utf-8"))
    log(f"プールP読み込み: {len(pool)}件  z*={z_star}")

    codes_needed = {r["code"] for r in pool}
    bars = load_bars_for(conn, codes_needed)

    T = cal.T
    idx_of = {d: i + 1 for i, d in enumerate(T)}

    def forward(code: str, date: str) -> dict:
        i0 = idx_of.get(date)
        if i0 is None:
            return {"status": "date_not_in_T"}
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
        vo1, o1, ul1, h1, ll1, l1 = row1.get("Vo"), row1.get("O"), row1.get("UL"), row1.get("H"), row1.get("LL"), row1.get("L")
        blocked, reason = False, "not_blocked"
        if vo1 is None or float(vo1) == 0.0:
            blocked, reason = True, "vo_zero"
        elif o1 is None:
            blocked, reason = True, "o_null"
        elif str(ul1) == "1" and h1 is not None and abs(float(o1) - float(h1)) <= 1e-6 * max(1.0, abs(float(h1))):
            blocked, reason = True, "ul_stop_high_open"
        elif str(ll1) == "1" and l1 is not None and abs(float(o1) - float(l1)) <= 1e-6 * max(1.0, abs(float(l1))):
            blocked, reason = True, "ll_stop_low_open_e8"
        adjo1, adjc5 = row1.get("AdjO"), row5.get("AdjC")
        r5 = None
        if adjo1 is not None and adjc5 is not None and float(adjo1) != 0:
            r5 = float(adjc5) / float(adjo1) - 1.0
        return {"status": "ok", "t1": t1, "t5": t5, "blocked": blocked, "blocked_reason": reason, "r5_gross": r5}

    log("前方リターン計算中...")
    status_counter = Counter()
    enriched = []
    for r in pool:
        fw = forward(r["code"], r["date"])
        status_counter[fw["status"]] += 1
        if fw["status"] != "ok" or fw.get("r5_gross") is None:
            continue
        enriched.append({**r, **fw})
    log(f"forward status={dict(status_counter)} 有効件数={len(enriched)}")

    excluded_version = [e for e in enriched if not e["blocked"]]
    log(f"約定不能除外後={len(excluded_version)}")

    sel_lo, sel_hi = cal_json["selection_range"]
    conf_lo, conf_hi = cal_json["confirmation_range"]

    # 市場超過リターン（同一t1窓のgapユニバース等加重平均R5）
    log("市場超過リターン計算中...")
    t1_set = sorted({e["t1"] for e in excluded_version})
    market_avg = {}
    for t1 in t1_set:
        i1 = idx_of[t1]
        t5 = cal.at(i1 + 4)
        if t5 is None:
            continue
        codes_u = uidx.codes_for(t1)
        rs = []
        for c in codes_u:
            r1 = bars.get(c, {}).get(t1)
            r5row = bars.get(c, {}).get(t5)
            if r1 is None or r5row is None:
                continue
            ao, ac = r1.get("AdjO"), r5row.get("AdjC")
            if ao and ac and float(ao) != 0:
                rs.append(float(ac) / float(ao) - 1.0)
        if rs:
            market_avg[t1] = sum(rs) / len(rs)
    for e in excluded_version:
        m = market_avg.get(e["t1"])
        e["r5_excess"] = (e["r5_gross"] - m) if m is not None else None

    def ic_bar_pool(events: list[dict], lo: str, hi: str) -> dict:
        by_date: dict[str, list[dict]] = defaultdict(list)
        for e in events:
            if lo <= e["date"] <= hi:
                by_date[e["date"]].append(e)
        day_ics, day_records = [], []
        for d, evs in by_date.items():
            if len(evs) < 5:
                continue
            valid = [e for e in evs if e["r5_excess"] is not None]
            if len(valid) < 5:
                continue
            ss = [e["S"] for e in valid]
            r5e = [e["r5_excess"] for e in valid]
            ic = spearman(ss, r5e)
            if ic is not None:
                day_ics.append(ic)
                day_records.append({"date": d, "n": len(valid), "ic": ic})
        ic_bar = (sum(day_ics) / len(day_ics)) if day_ics else None
        return {"ic_bar": ic_bar, "K": len(day_ics), "by_date_raw": by_date, "day_records": day_records}

    log("確認期間IC_bar（プールP）計算中...")
    conf_ic = ic_bar_pool(excluded_version, conf_lo, conf_hi)
    log(f"  IC_bar={conf_ic['ic_bar']} K={conf_ic['K']}")
    sel_ic = ic_bar_pool(excluded_version, sel_lo, sel_hi)

    log("permutation検定実行中...")
    rng = make_rng()
    by_date_conf = conf_ic["by_date_raw"]
    perm_days = []
    for d, evs in by_date_conf.items():
        valid = [e for e in evs if e["r5_excess"] is not None]
        if len(valid) >= 5:
            perm_days.append(([e["S"] for e in valid], [e["r5_excess"] for e in valid]))
    perm_obs = conf_ic["ic_bar"]
    perm_ics = []
    if perm_days and perm_obs is not None:
        for _ in range(N_PERM):
            day_ics = []
            for ss, r5e in perm_days:
                shuffled = ss[:]
                rng.shuffle(shuffled)
                ic = spearman(shuffled, r5e)
                if ic is not None:
                    day_ics.append(ic)
            if day_ics:
                perm_ics.append(sum(day_ics) / len(day_ics))
    ge_count = sum(1 for x in perm_ics if x >= perm_obs) if perm_obs is not None else None
    p_value = ((1 + ge_count) / (1 + len(perm_ics))) if perm_ics and ge_count is not None else None
    log(f"  p={p_value} reps={len(perm_ics)}")

    g1_1_pass = perm_obs is not None and perm_obs > 0 and p_value is not None and p_value < 0.05
    g1_2_pass = sel_ic["ic_bar"] is not None and sel_ic["ic_bar"] > 0

    # イベント集合E（確認期間・選定期間）: z<=z*
    event_conf = [e for e in excluded_version if conf_lo <= e["date"] <= conf_hi and e["z"] <= z_star]
    event_sel = [e for e in excluded_version if sel_lo <= e["date"] <= sel_hi and e["z"] <= z_star]

    def day_equal_weight_mean(events: list[dict], field: str) -> float | None:
        by_date = defaultdict(list)
        for e in events:
            if e.get(field) is not None:
                by_date[e["date"]].append(e[field])
        means = [sum(v) / len(v) for v in by_date.values()]
        return (sum(means) / len(means)) if means else None

    g1_3_value = day_equal_weight_mean(event_conf, "r5_gross")
    g1_3_pass = g1_3_value is not None and g1_3_value >= 0.0045

    g1_4_value = day_equal_weight_mean(event_conf, "r5_excess")
    g1_4_pass = g1_4_value is not None and g1_4_value > 0

    g1_6_value = day_equal_weight_mean(event_sel, "r5_gross")
    g1_6_pass = g1_6_value is not None and g1_6_value > 0

    # G1-5: プールのデシル単調性（Sの昇順10分割、各デシルmean R5_excess）
    log("デシル分析中...")
    pool_conf_valid = [e for e in excluded_version if conf_lo <= e["date"] <= conf_hi and e["r5_excess"] is not None]
    pool_conf_sorted = sorted(pool_conf_valid, key=lambda e: e["S"])
    n = len(pool_conf_sorted)
    decile_stats = {}
    decile_means = []
    for d in range(10):
        lo_i = int(n * d / 10)
        hi_i = int(n * (d + 1) / 10)
        chunk = pool_conf_sorted[lo_i:hi_i]
        vals = [e["r5_excess"] for e in chunk]
        vals_gross = [e["r5_gross"] for e in chunk]
        if vals:
            decile_stats[d + 1] = {"n": len(vals), "mean_excess": sum(vals) / len(vals), "mean_gross": sum(vals_gross) / len(vals_gross)}
            decile_means.append((d + 1, sum(vals) / len(vals)))
    rho_dec = spearman([p[0] for p in decile_means], [p[1] for p in decile_means]) if len(decile_means) >= 2 else None
    g1_5_pass = rho_dec is not None and rho_dec >= 0.30

    # G1-7: 単日依存性の除去（最大イベント日を除く）
    by_date_event_conf = Counter(e["date"] for e in event_conf)
    max_day = by_date_event_conf.most_common(1)[0][0] if by_date_event_conf else None
    excl_no_maxday = [e for e in excluded_version if e["date"] != max_day]
    ic_no_maxday = ic_bar_pool(excl_no_maxday, conf_lo, conf_hi)
    event_conf_no_maxday = [e for e in event_conf if e["date"] != max_day]
    r5_mean_no_maxday = day_equal_weight_mean(event_conf_no_maxday, "r5_gross")
    g1_7_pass = (ic_no_maxday["ic_bar"] is not None and ic_no_maxday["ic_bar"] > 0) and (r5_mean_no_maxday is not None and r5_mean_no_maxday > 0)

    all_g1_pass = g1_1_pass and g1_2_pass and g1_3_pass and g1_4_pass and g1_5_pass and g1_6_pass and g1_7_pass

    r5_conf_vals = [e["r5_gross"] for e in excluded_version if conf_lo <= e["date"] <= conf_hi]
    sigma_r_measured = None
    if len(r5_conf_vals) > 1:
        m = sum(r5_conf_vals) / len(r5_conf_vals)
        sigma_r_measured = (sum((v - m) ** 2 for v in r5_conf_vals) / (len(r5_conf_vals) - 1)) ** 0.5

    result = {
        "z_star": z_star,
        "G1-1_confirmation_IC_bar": conf_ic["ic_bar"], "G1-1_K": conf_ic["K"],
        "G1-1_permutation_p_value": p_value, "G1-1_permutation_reps": len(perm_ics), "G1-1_pass": g1_1_pass,
        "G1-2_selection_IC_bar": sel_ic["ic_bar"], "G1-2_selection_K": sel_ic["K"], "G1-2_pass": g1_2_pass,
        "G1-3_event_R5_gross_day_eq_weight_mean": g1_3_value, "G1-3_threshold": 0.0045, "G1-3_pass": g1_3_pass,
        "G1-4_event_R5_excess_day_eq_weight_mean": g1_4_value, "G1-4_pass": g1_4_pass,
        "G1-5_rho_dec": rho_dec, "G1-5_pass": g1_5_pass,
        "G1-6_selection_event_R5_gross_mean": g1_6_value, "G1-6_pass": g1_6_pass,
        "G1-7_max_day_excluded": max_day, "G1-7_IC_bar_excl": ic_no_maxday["ic_bar"],
        "G1-7_R5_gross_mean_excl": r5_mean_no_maxday, "G1-7_pass": g1_7_pass,
        "all_G1_pass": all_g1_pass,
        "decile_stats": decile_stats,
        "sigma_R_measured_confirmation": sigma_r_measured,
        "event_set_confirmation_count": len(event_conf),
        "event_set_selection_count": len(event_sel),
        "pool_confirmation_count": len(pool_conf_valid),
        "status_counter_forward_calc": dict(status_counter),
        "blocked_excluded_count": len(enriched) - len(excluded_version),
    }

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "prediction-unit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"saved: {RESULT_DIR / 'prediction-unit.json'}")
    log(json.dumps({k: v_ for k, v_ in result.items() if k.endswith("_pass")}, ensure_ascii=False, indent=2))
    return 0 if all_g1_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
