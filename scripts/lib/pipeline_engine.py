"""EXP-OBS000007/000006 共通のパイプライン統合（G2）シミュレーションエンジン。

日次イベントループでポジション管理（トレーリングストップ・単元離散化・
リスク集中上限・サーキットブレーカー）を行う。PEAD/Gapで異なるのは
シグナル生成・保有期間・トレールパラメータ・リスク上限の数値のみであり、
それらは呼び出し側が `PipelineConfig` として渡す。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class PipelineConfig:
    capital: float = 1_000_000.0
    target_position_jpy: float = 250_000.0
    position_cap_jpy: float = 375_000.0
    max_concurrent_positions: int = 5
    max_total_notional_jpy: float = 1_500_000.0
    max_same_sector_positions: int = 2
    max_new_per_signal_group: int = 3  # PEAD: 同一開示日3銘柄 / Gap: 同一イベント日2銘柄
    initial_stop_pct: float = -0.03
    trail_activation_pct: float = 0.04
    trail_follow_pct: float = -0.02
    max_holding_bd: int = 10  # t_1〜t_N。強制手仕舞いはt_{N+1}始値
    max_carryover_bd: int = 5
    buy_slippage: float = 0.00075
    sell_slippage: float = 0.00075
    spread_roundtrip: float = 0.0005
    commission_roundtrip: float = 0.0005
    margin_interest_annual: float = 0.028
    cb_halt_dd: float = 0.10
    cb_liquidate_dd: float = 0.15
    cb_resume_min_bd: int = 20
    cb_resume_recovery_dd: float = 0.10


@dataclass
class Position:
    code: str
    entry_i: int  # T index of t_1
    entry_date: str
    entry_price: float  # 約定価格（スリッページ込み）
    units: int
    notional: float
    sector: str | None
    signal_group: Any
    peak_close_since_entry: float
    trail_active: bool = False
    carryover_days: int = 0
    pending_exit_reason: str | None = None


def buy_blocked(row: dict) -> tuple[bool, str]:
    vo, o = row.get("Vo"), row.get("O")
    if vo is None or float(vo) == 0.0:
        return True, "vo_zero"
    if o is None:
        return True, "o_null"
    ul, h = row.get("UL"), row.get("H")
    if str(ul) == "1" and h is not None and abs(float(o) - float(h)) <= 1e-6 * max(1.0, abs(float(h))):
        return True, "ul_stop_high_open"
    return False, "not_blocked"


def sell_blocked(row: dict) -> tuple[bool, str]:
    vo, o = row.get("Vo"), row.get("O")
    if vo is None or float(vo) == 0.0:
        return True, "vo_zero"
    if o is None:
        return True, "o_null"
    ll, l_ = row.get("LL"), row.get("L")
    if str(ll) == "1" and l_ is not None and abs(float(o) - float(l_)) <= 1e-6 * max(1.0, abs(float(l_))):
        return True, "ll_stop_low_open"
    return False, "not_blocked"


def units_for(entry_ref_price: float, cfg: PipelineConfig) -> int | None:
    if entry_ref_price is None or entry_ref_price <= 0:
        return None
    u = max(1, round(cfg.target_position_jpy / (entry_ref_price * 100)))
    if u * entry_ref_price * 100 > cfg.position_cap_jpy:
        u -= 1
    if u <= 0 or u * entry_ref_price * 100 > cfg.position_cap_jpy:
        return None
    return u


def run_pipeline(
    T: list[str],
    i_start: int,
    i_end: int,
    signals_by_i: dict[int, list[dict]],
    bars_by_code: dict[str, dict[str, dict]],
    cfg: PipelineConfig,
    forced_close_check: Callable[[str, int, int], bool] | None = None,
) -> dict:
    """日次イベントループ本体。

    signals_by_i: {T_index(=t_1): [候補dict, ...]}。各候補は
      {code, sector, signal_group, ref_price_for_sizing(生値C(t0)), priority_key}
    forced_close_check(code, entry_i, current_i) -> bool: 決算跨ぎ等の強制手仕舞い判定（Trueなら手仕舞い）。

    戻り値: トレード記録・日次エクイティ・スロット競合・決済理由内訳等。
    """
    cash = cfg.capital
    open_positions: list[Position] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []
    slot_conflicts: list[dict] = []
    peak_equity = cfg.capital
    cb_halted = False
    cb_liquidate_pending = False
    cb_last_trigger_i: int | None = None
    cb_events: list[dict] = []

    def mark_to_market(i: int) -> float:
        date_i = T[i - 1]
        total = cash
        for p in open_positions:
            row = bars_by_code.get(p.code, {}).get(date_i)
            c = row.get("C") if row else None
            px = float(c) if c is not None else p.entry_price
            total += px * p.units * 100
        return total

    def close_position(p: Position, i: int, reason: str) -> bool:
        """i日の始値で決済を試みる。約定不能ならFalseを返し呼び出し側が持ち越し処理する。"""
        date_i = T[i - 1]
        row = bars_by_code.get(p.code, {}).get(date_i)
        if row is None:
            return False
        blocked, _breason = sell_blocked(row)
        if blocked:
            return False
        o = float(row["O"])
        exit_price = o * (1 - cfg.sell_slippage)
        gross_pnl = (exit_price - p.entry_price) * p.units * 100
        holding_bd = i - p.entry_i
        holding_cal_days = holding_bd  # 近似（暦日は営業日から概算しない。T添字差を暦日代用として記録）
        commission = p.notional * cfg.commission_roundtrip
        spread_cost = p.notional * cfg.spread_roundtrip
        interest = p.notional * cfg.margin_interest_annual * holding_bd / 365.0
        net_pnl = gross_pnl - commission - spread_cost - interest
        nonlocal cash
        cash += p.notional + net_pnl
        trades.append({
            "code": p.code, "entry_date": p.entry_date, "exit_date": date_i,
            "entry_price": p.entry_price, "exit_price": exit_price, "units": p.units,
            "notional": p.notional, "gross_pnl": gross_pnl, "net_pnl": net_pnl,
            "commission": commission, "spread_cost": spread_cost, "interest": interest,
            "holding_bd": holding_bd, "exit_reason": reason,
            "return_on_notional_gross": gross_pnl / p.notional if p.notional else None,
            "return_on_notional_net": net_pnl / p.notional if p.notional else None,
        })
        return True

    exit_reason_counter: dict[str, int] = {}

    for i in range(i_start, i_end + 1):
        date_i = T[i - 1]
        equity_before = mark_to_market(i)

        # --- サーキットブレーカー判定（前日終値ベースのエクイティで評価） ---
        if equity_before > peak_equity:
            peak_equity = equity_before
        dd = (peak_equity - equity_before) / peak_equity if peak_equity else 0.0

        if cb_liquidate_pending:
            still_open = []
            for p in open_positions:
                if close_position(p, i, "circuit_breaker_liquidate"):
                    exit_reason_counter["circuit_breaker_liquidate"] = exit_reason_counter.get("circuit_breaker_liquidate", 0) + 1
                else:
                    p.carryover_days += 1
                    if p.carryover_days >= cfg.max_carryover_bd:
                        still_open.append(p)  # 強制評価は別途下部の共通処理へ委ねず、ここでは維持のみ
                    else:
                        still_open.append(p)
            open_positions = still_open
            cb_liquidate_pending = False

        if dd >= cfg.cb_liquidate_dd and cb_last_trigger_i != i:
            cb_events.append({"type": "liquidate_15pct", "date": date_i, "i": i, "dd": dd})
            cb_halted = True
            cb_liquidate_pending = True
            cb_last_trigger_i = i
        elif dd >= cfg.cb_halt_dd:
            if not cb_halted:
                cb_events.append({"type": "halt_10pct", "date": date_i, "i": i, "dd": dd})
            cb_halted = True
        elif cb_halted and dd < cfg.cb_resume_recovery_dd:
            last_liq = next((e for e in reversed(cb_events) if e["type"] == "liquidate_15pct"), None)
            if last_liq is None or (i - last_liq["i"]) >= cfg.cb_resume_min_bd:
                cb_halted = False
                cb_events.append({"type": "resume", "date": date_i, "i": i, "dd": dd})

        # --- 決済判定（既存ポジション） ---
        still_open = []
        for p in open_positions:
            row_today = bars_by_code.get(p.code, {}).get(date_i)
            reason = None
            if row_today is not None:
                c_today = row_today.get("C")
                if c_today is not None:
                    c_today = float(c_today)
                    if c_today > p.peak_close_since_entry:
                        p.peak_close_since_entry = c_today
                    gain = c_today / p.entry_price - 1.0
                    if not p.trail_active and gain >= cfg.trail_activation_pct:
                        p.trail_active = True  # 翌営業日から有効化（当日はまだ判定しない）
                    elif p.trail_active:
                        drawdown_from_peak = c_today / p.peak_close_since_entry - 1.0
                        if drawdown_from_peak <= cfg.trail_follow_pct:
                            reason = "trail_stop"
                    if reason is None and c_today <= p.entry_price * (1 + cfg.initial_stop_pct):
                        reason = "initial_stop"
            if forced_close_check is not None and reason is None and forced_close_check(p.code, p.entry_i, i):
                reason = "earnings_straddle_avoidance"
            if reason is None and (i - p.entry_i) >= cfg.max_holding_bd:
                reason = "max_holding_time_out"

            if reason is not None:
                p.pending_exit_reason = reason

            if p.pending_exit_reason is not None:
                ok = close_position(p, i, p.pending_exit_reason)
                if ok:
                    exit_reason_counter[p.pending_exit_reason] = exit_reason_counter.get(p.pending_exit_reason, 0) + 1
                    continue
                else:
                    p.carryover_days += 1
                    if p.carryover_days >= cfg.max_carryover_bd:
                        # 強制評価（当日終値で評価損益を確定）
                        row = bars_by_code.get(p.code, {}).get(date_i)
                        c = row.get("C") if row else None
                        px = float(c) if c is not None else p.entry_price
                        gross_pnl = (px - p.entry_price) * p.units * 100
                        cash += p.notional + gross_pnl
                        trades.append({
                            "code": p.code, "entry_date": p.entry_date, "exit_date": date_i,
                            "entry_price": p.entry_price, "exit_price": px, "units": p.units,
                            "notional": p.notional, "gross_pnl": gross_pnl, "net_pnl": gross_pnl,
                            "holding_bd": i - p.entry_i, "exit_reason": "forced_valuation_carryover_limit",
                            "return_on_notional_gross": gross_pnl / p.notional if p.notional else None,
                            "return_on_notional_net": gross_pnl / p.notional if p.notional else None,
                        })
                        exit_reason_counter["forced_valuation_carryover_limit"] = exit_reason_counter.get("forced_valuation_carryover_limit", 0) + 1
                        continue
                    still_open.append(p)
            else:
                still_open.append(p)
        open_positions = still_open

        # --- 新規建て（当日がエントリー日t_1の候補） ---
        candidates = list(signals_by_i.get(i, []))
        if candidates and not cb_halted:
            candidates.sort(key=lambda c: c["priority_key"])
            sector_counts: dict[str, int] = {}
            group_counts: dict[Any, int] = {}
            for p in open_positions:
                if p.sector is not None:
                    sector_counts[p.sector] = sector_counts.get(p.sector, 0) + 1
                group_counts[p.signal_group] = group_counts.get(p.signal_group, 0) + 1
            current_notional = sum(p.notional for p in open_positions)
            for cand in candidates:
                row = bars_by_code.get(cand["code"], {}).get(date_i)
                if row is None:
                    slot_conflicts.append({**cand, "reason": "no_bar_row"})
                    continue
                blocked, breason = buy_blocked(row)
                if blocked:
                    slot_conflicts.append({**cand, "reason": f"buy_blocked_{breason}"})
                    continue
                if len(open_positions) >= cfg.max_concurrent_positions:
                    slot_conflicts.append({**cand, "reason": "max_concurrent_positions"})
                    continue
                sec = cand.get("sector")
                if sec is not None and sector_counts.get(sec, 0) >= cfg.max_same_sector_positions:
                    slot_conflicts.append({**cand, "reason": "max_same_sector"})
                    continue
                if group_counts.get(cand["signal_group"], 0) >= cfg.max_new_per_signal_group:
                    slot_conflicts.append({**cand, "reason": "max_same_signal_group"})
                    continue
                units = units_for(cand["ref_price_for_sizing"], cfg)
                if units is None:
                    slot_conflicts.append({**cand, "reason": "sizing_infeasible"})
                    continue
                o = float(row["O"])
                entry_price = o * (1 + cfg.buy_slippage)
                notional = entry_price * units * 100
                if current_notional + notional > cfg.max_total_notional_jpy:
                    slot_conflicts.append({**cand, "reason": "max_total_notional"})
                    continue
                cash -= notional
                current_notional += notional
                sector_counts[sec] = sector_counts.get(sec, 0) + 1
                group_counts[cand["signal_group"]] = group_counts.get(cand["signal_group"], 0) + 1
                open_positions.append(Position(
                    code=cand["code"], entry_i=i, entry_date=date_i, entry_price=entry_price,
                    units=units, notional=notional, sector=sec, signal_group=cand["signal_group"],
                    peak_close_since_entry=entry_price,
                ))

        equity_after = mark_to_market(i)
        equity_curve.append({"date": date_i, "i": i, "equity": equity_after, "open_positions": len(open_positions), "cb_halted": cb_halted})

    max_dd = 0.0
    peak = cfg.capital
    for e in equity_curve:
        if e["equity"] > peak:
            peak = e["equity"]
        dd = (peak - e["equity"]) / peak if peak else 0.0
        if dd > max_dd:
            max_dd = dd

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "slot_conflicts": slot_conflicts,
        "exit_reason_counter": exit_reason_counter,
        "max_drawdown": max_dd,
        "final_equity": equity_curve[-1]["equity"] if equity_curve else cfg.capital,
        "cb_events": cb_events,
        "open_positions_at_end": len(open_positions),
    }
