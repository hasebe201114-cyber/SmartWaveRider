#!/usr/bin/env python3
"""EXP-OBS000008（ギャップ・10年版）§4〜§5・§6.4〜§6.5 G2（パイプライン統合）測定。

前提: `gap10y_prediction_unit.py` がG1全合格していること（N-12）。

出力: `research/EXP-OBS000008/10-result/pipeline.json` / `cost-model.json`
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jq10y_common import DB_PATH, RAW_DIR, Calendar, UniverseIndex, load_calendar, load_universe  # noqa: E402
from lib.pipeline_engine import PipelineConfig, run_pipeline  # noqa: E402

RESULT_DIR = Path(__file__).resolve().parent.parent / "research" / "EXP-OBS000008" / "10-result"
U6_CAP_LABEL = "gap"

COST_MODEL = {
    "spread_roundtrip_pct": 0.0010,
    "slippage_roundtrip_pct": 0.0020,
    "commission_roundtrip_pct": 0.0010,
    "margin_interest_annual_pct_component_roundtrip": 0.0005,
    "roundtrip_total_at_5bd_holding_pct": 0.0045,
    "status": "spec §8.1（旧spec§8と一字一句同一）。実測値ではなく事前固定の仮置き前提",
    "sensitivity_levels_pct": [0.0035, 0.0045, 0.0060],
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    pu = json.loads((RESULT_DIR / "prediction-unit.json").read_text(encoding="utf-8"))
    if not pu.get("all_G1_pass"):
        log("STOP: G1未達のためG2を測定しない（N-12）。")
        return 3
    z_star = pu["z_star"]

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA query_only=ON")
    cal_json = load_calendar()
    cal = Calendar(cal_json)
    universe_json = load_universe()
    uidx = UniverseIndex(universe_json, U6_CAP_LABEL)

    pool = json.loads((RESULT_DIR / "gap_pool.json").read_text(encoding="utf-8"))
    candidates_raw = [r for r in pool if r["z"] is not None and r["z"] <= z_star]
    log(f"イベント集合E件数（全期間）={len(candidates_raw)}  z*={z_star}")

    T = cal.T
    idx_of = {d: i + 1 for i, d in enumerate(T)}

    master_cache: dict[str, dict[str, str]] = {}

    def sector_for(code: str, event_date: str) -> str | None:
        gd = uidx.governing_date(event_date)
        if gd is None:
            return None
        if gd not in master_cache:
            fp = RAW_DIR / "master_by_date" / f"{gd}.json"
            m = {}
            if fp.exists():
                recs = json.loads(fp.read_text(encoding="utf-8"))
                m = {r["Code"]: r.get("S33") for r in recs}
            master_cache[gd] = m
        return master_cache[gd].get(code)

    codes_needed = {r["code"] for r in candidates_raw}
    bars_by_code: dict[str, dict[str, dict]] = {}
    for i in range(0, len(codes_needed), 500):
        sub = list(codes_needed)[i:i + 500]
        rows2 = conn.execute(
            f"SELECT code,date,o,c,ul,ll,h,l,vo FROM bars WHERE code IN ({','.join('?'*len(sub))})", sub
        ).fetchall()
        for code, date, o, c, ul, ll, h, l, vo in rows2:
            bars_by_code.setdefault(code, {})[date] = {"O": o, "C": c, "UL": ul, "LL": ll, "H": h, "L": l, "Vo": vo}

    signals_by_i: dict[int, list[dict]] = defaultdict(list)
    skipped_no_cd = 0
    e8_excluded = 0
    for r in candidates_raw:
        i0 = idx_of.get(r["date"])
        if i0 is None:
            continue
        entry_i = i0 + 1
        if entry_i > cal_json["T_len"]:
            continue
        row_d = bars_by_code.get(r["code"], {}).get(r["date"])
        if row_d is None or row_d.get("C") is None:
            skipped_no_cd += 1
            continue
        # E-8（本EXP固有の追加除外）: LL(t1)=1 かつ O(t1)=L(t1) は見送る
        t1_date = T[entry_i - 1] if 1 <= entry_i <= len(T) else None
        row_t1 = bars_by_code.get(r["code"], {}).get(t1_date) if t1_date else None
        if row_t1 is not None and str(row_t1.get("LL")) == "1" and row_t1.get("L") is not None and row_t1.get("O") is not None:
            if abs(float(row_t1["O"]) - float(row_t1["L"])) <= 1e-6 * max(1.0, abs(float(row_t1["L"]))):
                e8_excluded += 1
                continue
        signals_by_i[entry_i].append({
            "code": r["code"], "sector": sector_for(r["code"], r["date"]),
            "signal_group": r["date"], "ref_price_for_sizing": float(row_d["C"]),
            "priority_key": (-r["S"], r["code"]),
        })
    log(f"エントリー候補件数={sum(len(v) for v in signals_by_i.values())}（C(D)欠損除外={skipped_no_cd} E-8除外={e8_excluded}）")

    log("決算跨ぎ制約（H-3字義通り）用データ読み込み中...")
    ed_rows = conn.execute("SELECT pubdate, schdate, code FROM earnings_date").fetchall()
    ed_by_code: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pub, sch, code in ed_rows:
        if pub and sch:
            ed_by_code[code].append((pub, sch))
    for code in ed_by_code:
        ed_by_code[code].sort()

    def forced_close_check(code: str, entry_i: int, current_i: int) -> bool:
        date_current = T[current_i - 1]
        recs = ed_by_code.get(code)
        if not recs:
            return False
        latest_sch = None
        for pub, sch in recs:
            if pub <= date_current:
                latest_sch = sch
            else:
                break
        if latest_sch is None:
            return False
        t1 = T[entry_i - 1] if 1 <= entry_i <= len(T) else None
        t6 = T[min(entry_i + 5, len(T)) - 1]
        # Eの前営業日引けで手仕舞い決定 -> E当日始値で約定。ここでは判定日=E前営業日を検出する近似実装。
        if t1 is None:
            return False
        i_sch = idx_of.get(latest_sch)
        if i_sch is None:
            return False
        return t1 <= latest_sch <= t6 and (i_sch - 1) == current_i

    cfg = PipelineConfig(
        capital=1_000_000, target_position_jpy=250_000, position_cap_jpy=375_000,
        max_concurrent_positions=5, max_total_notional_jpy=1_250_000, max_same_sector_positions=2,
        max_new_per_signal_group=2, initial_stop_pct=-0.04, trail_activation_pct=0.04,
        trail_follow_pct=-0.02, max_holding_bd=5, max_carryover_bd=5,
        buy_slippage=0.00100, sell_slippage=0.00100, spread_roundtrip=0.0010,
        commission_roundtrip=0.0010, margin_interest_annual=0.028,
        cb_halt_dd=0.10, cb_liquidate_dd=0.15, cb_resume_min_bd=20, cb_resume_recovery_dd=0.10,
    )

    i_start, i_end = cal_json["T68_idx"], cal_json["T_len"]
    log(f"パイプラインシミュレーション実行中: i={i_start}〜{i_end}...")
    result = run_pipeline(T, i_start, i_end, signals_by_i, bars_by_code, cfg, forced_close_check)
    log(f"トレード数={len(result['trades'])} 最大DD={result['max_drawdown']:.4f} 最終エクイティ={result['final_equity']:.0f}")

    sel_lo, sel_hi = cal_json["selection_range"]
    conf_lo, conf_hi = cal_json["confirmation_range"]
    trades = result["trades"]
    conf_trades = [t for t in trades if conf_lo <= t["entry_date"] <= conf_hi]
    sel_trades = [t for t in trades if sel_lo <= t["entry_date"] <= sel_hi]

    def avg(vals):
        return sum(vals) / len(vals) if vals else None

    conf_net_returns = [t["return_on_notional_net"] for t in conf_trades if t["return_on_notional_net"] is not None]
    sel_net_returns = [t["return_on_notional_net"] for t in sel_trades if t["return_on_notional_net"] is not None]

    g2_1_value = avg(conf_net_returns)
    g2_1_pass = g2_1_value is not None and g2_1_value > 0.0
    cumulative_net_return = (result["final_equity"] - cfg.capital) / cfg.capital
    g2_2_pass = cumulative_net_return > 0.0
    g2_3_sel_value = avg(sel_net_returns)
    g2_3_pass = (g2_1_value is not None and g2_1_value > 0) and (g2_3_sel_value is not None and g2_3_sel_value > 0)
    g2_4_pass = result["max_drawdown"] <= 0.15
    g2_5_pass = len(conf_trades) >= 30

    total_signals_conf = sum(len(v) for i, v in signals_by_i.items() if conf_lo <= T[i - 1] <= conf_hi)
    buy_blocked_conf = sum(
        1 for sc in result["slot_conflicts"]
        if sc.get("reason", "").startswith("buy_blocked") and conf_lo <= sc.get("signal_group", "") <= conf_hi
    )
    g2_6_value = (buy_blocked_conf / total_signals_conf) if total_signals_conf else None
    g2_6_pass = g2_6_value is not None and g2_6_value <= 0.50

    all_g2_pass = g2_1_pass and g2_2_pass and g2_3_pass and g2_4_pass and g2_5_pass and g2_6_pass

    # G2-7: 往復コスト0.60%ケース（片道スリッページ0.100%→0.175%、他は固定。spec §8.2）
    log("G2-7（往復0.60%ケース）再シミュレーション中...")
    cfg_high_cost = PipelineConfig(**{**cfg.__dict__, "buy_slippage": 0.00175, "sell_slippage": 0.00175})
    result_hc = run_pipeline(T, i_start, i_end, signals_by_i, bars_by_code, cfg_high_cost, forced_close_check)
    conf_trades_hc = [t for t in result_hc["trades"] if conf_lo <= t["entry_date"] <= conf_hi]
    g2_7_value = avg([t["return_on_notional_net"] for t in conf_trades_hc if t["return_on_notional_net"] is not None])
    g2_7_pass = g2_7_value is not None and g2_7_value > 0.0

    exit_breakdown = result["exit_reason_counter"]
    slot_conflict_reasons = Counter(sc.get("reason") for sc in result["slot_conflicts"])

    eq = [e["equity"] for e in result["equity_curve"]]
    daily_rets = [(eq[i] / eq[i - 1] - 1.0) for i in range(1, len(eq)) if eq[i - 1]]
    sharpe_annual = None
    sharpe_ci95 = None
    if len(daily_rets) > 2:
        m = sum(daily_rets) / len(daily_rets)
        sd = (sum((x - m) ** 2 for x in daily_rets) / (len(daily_rets) - 1)) ** 0.5
        if sd > 0:
            sharpe_daily = m / sd
            sharpe_annual = sharpe_daily * (245 ** 0.5)
            n_years = len(daily_rets) / 245.0
            se = (1.0 / n_years) ** 0.5 if n_years > 0 else None
            sharpe_ci95 = [sharpe_annual - 1.96 * se, sharpe_annual + 1.96 * se] if se else None

    pipeline_result = {
        "z_star": z_star,
        "G2-1_confirmation_avg_net_return": g2_1_value, "G2-1_pass": g2_1_pass,
        "G2-2_cumulative_net_return": cumulative_net_return, "G2-2_pass": g2_2_pass,
        "G2-3_selection_avg_net_return": g2_3_sel_value, "G2-3_pass": g2_3_pass,
        "G2-4_max_drawdown": result["max_drawdown"], "G2-4_threshold": 0.15, "G2-4_pass": g2_4_pass,
        "G2-5_confirmation_trade_count": len(conf_trades), "G2-5_threshold": 30, "G2-5_pass": g2_5_pass,
        "G2-6_unfillable_rate": g2_6_value, "G2-6_threshold": 0.50, "G2-6_pass": g2_6_pass,
        "G2-7_confirmation_avg_net_return_at_060pct_roundtrip": g2_7_value, "G2-7_pass": g2_7_pass,
        "all_G2_pass": all_g2_pass and g2_7_pass,
        "trade_count_total": len(trades), "trade_count_confirmation": len(conf_trades), "trade_count_selection": len(sel_trades),
        "exit_reason_breakdown": exit_breakdown,
        "slot_conflict_reason_breakdown": dict(slot_conflict_reasons),
        "final_equity": result["final_equity"],
        "cb_events": result["cb_events"],
        "candidates_total": len(candidates_raw),
        "candidates_c_d_missing_skipped": skipped_no_cd,
        "sharpe_annualized_reference_only": sharpe_annual,
        "sharpe_annualized_95ci_reference_only": sharpe_ci95,
    }

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "pipeline.json").write_text(json.dumps(pipeline_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (RESULT_DIR / "cost-model.json").write_text(json.dumps(COST_MODEL, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"saved: {RESULT_DIR / 'pipeline.json'}")
    log(json.dumps({k: v for k, v in pipeline_result.items() if k.endswith("_pass")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
