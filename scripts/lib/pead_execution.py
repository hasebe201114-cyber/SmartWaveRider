#!/usr/bin/env python3
"""EXP-OBS000001 約定モデル・ポジション管理。spec §4・§6.1・§6.3・§6.4 の実装。

API を一切呼ばない純粋関数群。日足バー配列（`Date,O,H,L,C,UL,LL,Vo,Va` を
持つ dict のリスト。銘柄内で日付昇順）を入力として、1トレードの経路依存な
決済シミュレーション（§4.4）、単元離散化（§6.1）、スロット競合を伴う
ポートフォリオ配分（§6.3）を行う。

**設計原則（spec §4.4 冒頭）**: 有利方向の判定は終値のみ（＝遅い）、
不利方向の判定は日中値（＝早い）。日中高値はトレール切り上げに一切使わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

ENTRY_SLIPPAGE = 0.00075  # 片道0.075%（spec §4.2）
INITIAL_STOP_PCT = 0.030  # 初期損切幅 -3.0%（spec §4.3 #1）
TRAIL_TRIGGER_PCT = 0.030  # 発動水準 +3.0%（終値ベース）（spec §4.3 #2）
TRAIL_TRAIL_PCT = 0.030  # 追随幅 -3.0%（spec §4.3 #3）
STOP_EXECUTION_EXTRA_SLIPPAGE = 0.0010  # ストップ執行時の追加スリッページ（spec §4.6）
MAX_HOLD_BARS = 10  # D+10 で時間切れ（spec §4.1）
TARGET_NOTIONAL = 250_000.0  # 目標建玉25万円（spec §6.1）
LOT_SIZE = 100  # 単元株


class SettlementBucket(str, Enum):
    STOP_LOSS = "stop_loss"          # トレール未発動のまま Step1/Step2 で決済
    TRAIL_EXIT = "trail_exit"        # トレール発動後に Step1/Step2 で決済
    TIME_EXIT = "time_exit"          # D+10 引けで決済
    FORCED_EARNINGS_EXIT = "forced_earnings_exit"  # 次回決算前営業日引けでの強制手仕舞い（§6.4）
    UNFILLED_ENTRY = "unfilled_entry"  # D+1寄りが約定不能（§4.2）


class EntryFillOutcome(str, Enum):
    FILLED = "filled"
    UNFILLED_LIMIT_UP = "unfilled_limit_up"  # O_D+1 == UL_D+1
    UNFILLED_NO_VOLUME = "unfilled_no_volume"  # Vo_D+1 == 0


@dataclass
class EntryResult:
    outcome: EntryFillOutcome
    entry_price: float | None  # 約定価格（スリッページ込み）。未約定ならNone
    entry_bar: dict | None
    counterfactual_h5_gross_return: float | None = None  # 未約定時: O_{D+2}で建てた場合のh=5グロス


def determine_entry(bars_after_disc_date: list[dict]) -> EntryResult:
    """spec §4.2: D+1寄りエントリーの成否判定。

    `bars_after_disc_date` は当該銘柄の DiscDate より後の日足バー（昇順）。
    先頭が D+1。
    """
    if not bars_after_disc_date:
        return EntryResult(outcome=EntryFillOutcome.UNFILLED_NO_VOLUME, entry_price=None, entry_bar=None)
    d1 = bars_after_disc_date[0]
    o, ul, vo = d1.get("O"), d1.get("UL"), d1.get("Vo")
    unfilled: EntryFillOutcome | None = None
    if o is not None and ul is not None and float(o) == float(ul):
        unfilled = EntryFillOutcome.UNFILLED_LIMIT_UP
    elif vo is not None and float(vo) == 0:
        unfilled = EntryFillOutcome.UNFILLED_NO_VOLUME

    if unfilled is not None:
        counterfactual = None
        # 反実仮想: 仮に O_{D+2} で建てたとした場合の h=5 グロスリターン（spec §5.5 #2）。
        # D+2を起点にD+2..D+6の5本を使う（正常系のD+1..D+5と同じ「エントリー日を含めた5本」対応）。
        if len(bars_after_disc_date) >= 6:
            cf_entry = bars_after_disc_date[1]
            cf_exit = bars_after_disc_date[5]
            if cf_entry.get("O") and cf_exit.get("C"):
                counterfactual = float(cf_exit["C"]) / float(cf_entry["O"]) - 1.0
        return EntryResult(
            outcome=unfilled, entry_price=None, entry_bar=d1, counterfactual_h5_gross_return=counterfactual
        )

    entry_price = float(o) * (1.0 + ENTRY_SLIPPAGE)
    return EntryResult(outcome=EntryFillOutcome.FILLED, entry_price=entry_price, entry_bar=d1)


@dataclass
class DailyStep:
    date: str
    close: float
    stop_level: float
    trail_active: bool
    high_watermark: float | None


@dataclass
class TradeSimResult:
    bucket: SettlementBucket
    entry_price: float
    exit_price: float | None
    exit_date: str | None
    n_bars_held: int
    trail_activated: bool
    daily_trace: list[DailyStep] = field(default_factory=list)
    carried_over_extra_days: int = 0  # §6.2 値幅制限による持ち越し追加日数


def simulate_trailing_stop_trade(
    entry_price: float,
    bars_after_entry: list[dict],
    *,
    forced_exit_before_date: str | None = None,
) -> TradeSimResult:
    """spec §4.3・§4.4 のトレーリングストップ日次ループ。

    `bars_after_entry` は D+1 (エントリー当日) から始まる日足バー配列（昇順）。
    `forced_exit_before_date` を渡すと、その日の**前営業日引け**で強制手仕舞いする
    （spec §6.4: 未発表の次回決算発表予定日を跨がない）。日付文字列（ISO8601）で比較する。
    """
    E = entry_price
    S = E * (1.0 - INITIAL_STOP_PCT)
    HW: float | None = None
    trail_active = False
    trace: list[DailyStep] = []
    carried_over_extra_days = 0

    bars = bars_after_entry[:MAX_HOLD_BARS]
    i = 0
    while i < len(bars):
        d = bars[i]
        date = str(d.get("Date"))
        o, h, l, c = d.get("O"), d.get("H"), d.get("L"), d.get("C")
        ll = d.get("LL")
        is_last_normal_bar = i == MAX_HOLD_BARS - 1  # D+10 相当

        # §6.4: 次回決算発表予定日の前営業日引けで強制手仕舞い。
        # 判定は「今日の引け時点で、次のバー日がforced_exit_before_date以降か」で行う。
        next_date = str(bars[i + 1].get("Date")) if i + 1 < len(bars) else None
        forced_exit_today = (
            forced_exit_before_date is not None
            and next_date is not None
            and next_date >= forced_exit_before_date
        )

        # Step 1: 寄り・ギャップ判定
        if o is not None and S is not None and float(o) <= S:
            if ll is not None and float(o) == float(ll):
                # ストップ安で寄り、当日約定不能 → 翌営業日へ持ち越し（§6.2）
                carried_over_extra_days += 1
                i += 1
                continue
            exit_price = float(o)
            bucket = SettlementBucket.TRAIL_EXIT if trail_active else SettlementBucket.STOP_LOSS
            trace.append(DailyStep(date, float(c) if c is not None else float("nan"), S, trail_active, HW))
            return TradeSimResult(
                bucket=bucket,
                entry_price=E,
                exit_price=exit_price,
                exit_date=date,
                n_bars_held=i + 1,
                trail_activated=trail_active,
                daily_trace=trace,
                carried_over_extra_days=carried_over_extra_days,
            )

        # Step 2: 日中・ストップヒット判定（不利方向は日中安値で早く判定）
        if l is not None and S is not None and float(l) <= S:
            if ll is not None and float(l) == float(ll) and c is not None and float(c) == float(ll):
                # ストップ安比例配分で当日約定不能 → 翌営業日へ持ち越し（§6.2）
                carried_over_extra_days += 1
                i += 1
                continue
            exit_price = S * (1.0 - STOP_EXECUTION_EXTRA_SLIPPAGE)
            bucket = SettlementBucket.TRAIL_EXIT if trail_active else SettlementBucket.STOP_LOSS
            trace.append(DailyStep(date, float(c) if c is not None else float("nan"), S, trail_active, HW))
            return TradeSimResult(
                bucket=bucket,
                entry_price=E,
                exit_price=exit_price,
                exit_date=date,
                n_bars_held=i + 1,
                trail_activated=trail_active,
                daily_trace=trace,
                carried_over_extra_days=carried_over_extra_days,
            )

        # Step 3: 時間切れ判定（D+10 引け）
        if is_last_normal_bar and c is not None:
            trace.append(DailyStep(date, float(c), S, trail_active, HW))
            return TradeSimResult(
                bucket=SettlementBucket.TIME_EXIT,
                entry_price=E,
                exit_price=float(c),
                exit_date=date,
                n_bars_held=i + 1,
                trail_activated=trail_active,
                daily_trace=trace,
                carried_over_extra_days=carried_over_extra_days,
            )

        # §6.4: 強制手仕舞い（次回決算発表予定日の前営業日引け）
        if forced_exit_today and c is not None:
            trace.append(DailyStep(date, float(c), S, trail_active, HW))
            return TradeSimResult(
                bucket=SettlementBucket.FORCED_EARNINGS_EXIT,
                entry_price=E,
                exit_price=float(c),
                exit_date=date,
                n_bars_held=i + 1,
                trail_activated=trail_active,
                daily_trace=trace,
                carried_over_extra_days=carried_over_extra_days,
            )

        # Step 4: 引け・トレール水準の更新（終値のみ。日中高値は使わない）
        if c is not None:
            c = float(c)
            gain = c / E - 1.0
            if not trail_active and gain >= TRAIL_TRIGGER_PCT:
                trail_active = True
                HW = c
            if trail_active:
                HW = max(HW, c) if HW is not None else c
                S = max(S, HW * (1.0 - TRAIL_TRAIL_PCT))
            trace.append(DailyStep(date, c, S, trail_active, HW))

        i += 1

    # ループを抜けた（=バーが尽きた。上場廃止・データ末尾等で D+10 未満）。
    # 呼び出し側で「バー不足イベント」として G1/G2 両方から除外すること（spec §4.1）。
    last = bars[-1] if bars else None
    return TradeSimResult(
        bucket=SettlementBucket.TIME_EXIT,
        entry_price=E,
        exit_price=float(last["C"]) if last and last.get("C") is not None else None,
        exit_date=str(last.get("Date")) if last else None,
        n_bars_held=len(bars),
        trail_activated=trail_active,
        daily_trace=trace,
        carried_over_extra_days=carried_over_extra_days,
    )


@dataclass
class DiscretizationResult:
    shares: int
    actual_notional: float
    deviation_rate: float  # actual/target - 1
    effective_f: float  # 単元離散化後の1トレード最大損失(=定義上の初期損切額) ÷ 資本


def discretize_position(entry_price: float, *, capital: float, target_notional: float = TARGET_NOTIONAL) -> DiscretizationResult:
    """spec §6.1: 単元100株の離散化。`floor(250000 / (P*100)) * 100`。

    浮動小数点誤差で本来整数のはずの商が僅かに0.999...側へぶれ、floorが
    1段階小さく出るのを防ぐため、floor前に小数第9位で丸める。
    """
    import math

    lots = math.floor(round(target_notional / (entry_price * LOT_SIZE), 9))
    shares = max(LOT_SIZE, lots * LOT_SIZE)
    actual_notional = shares * entry_price
    deviation_rate = actual_notional / target_notional - 1.0
    effective_f = (actual_notional * INITIAL_STOP_PCT) / capital
    return DiscretizationResult(
        shares=shares, actual_notional=actual_notional, deviation_rate=deviation_rate, effective_f=effective_f
    )


@dataclass
class SlotAllocationConfig:
    max_concurrent_positions: int = 8
    max_new_entries_per_day: int = 3
    max_concurrent_per_sector: int = 3


@dataclass
class CandidateSignal:
    code: str
    disc_date: str
    entry_date: str  # D+1 の日付
    raw_sue: float
    sector_s33: str
    exit_date: str  # トレードシミュレーション結果の決済日（スロット占有解放の判定に使う）


def allocate_slots(
    candidates: list[CandidateSignal], config: SlotAllocationConfig = SlotAllocationConfig()
) -> tuple[list[CandidateSignal], list[CandidateSignal]]:
    """spec §6.3: スロット競合の解決。同時保有上限8・1日新規建て上限3・同一33業種上限3。

    優先順位: RawSUE降順、同値は銘柄コード昇順（決定的）。
    戻り値: (建てられた候補, 見送られた候補)
    """
    by_entry_date: dict[str, list[CandidateSignal]] = {}
    for c in candidates:
        by_entry_date.setdefault(c.entry_date, []).append(c)

    open_positions: list[CandidateSignal] = []  # 現在保有中（entry_date <= today < exit_date）
    accepted: list[CandidateSignal] = []
    rejected: list[CandidateSignal] = []

    for entry_date in sorted(by_entry_date.keys()):
        # 当日以前に決済済みのポジションを開放
        open_positions = [p for p in open_positions if p.exit_date > entry_date]

        todays_candidates = sorted(by_entry_date[entry_date], key=lambda c: (-c.raw_sue, c.code))
        new_today = 0
        for cand in todays_candidates:
            sector_count = sum(1 for p in open_positions if p.sector_s33 == cand.sector_s33)
            if (
                len(open_positions) < config.max_concurrent_positions
                and new_today < config.max_new_entries_per_day
                and sector_count < config.max_concurrent_per_sector
            ):
                open_positions.append(cand)
                accepted.append(cand)
                new_today += 1
            else:
                rejected.append(cand)

    return accepted, rejected
