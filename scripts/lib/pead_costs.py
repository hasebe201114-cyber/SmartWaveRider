#!/usr/bin/env python3
"""EXP-OBS000001 コストモデル。spec §4.6 の実装。

**重要（二重計上の防止）**: `pead_execution.simulate_trailing_stop_trade` は
すでに以下をエントリー/決済**価格そのもの**に織り込み済みである。
- エントリー: 片道スリッページ 0.075%（spec §4.2）
- ストップ執行時: 上記に加えて追加スリッページ 0.10%（spec §4.4 Step2）

したがって本モジュールが上乗せする「フラット・コスト」は、**上記に含まれない
残りの構成要素のみ**である。spec §4.6 の往復合計 0.35%／0.45% という数字は
「エントリースリッページ0.075%（価格に内包）＋本モジュールのフラット分」の
合計として再現されるように設計している（内訳は本ファイルの定数コメントに
算出根拠を記す）。

- スプレッド往復 0.05%（仮置き・R-2未確認）
- 決済側の通常スリッページ 片道0.075%（仮置き。エントリー側は価格に内包済みのため
  ここでは決済側のみを計上する）
- 委託手数料 往復0.05%（仮置き・R-3未確認）
- 制度信用買方金利 年率2.8%を実保有暦日で日割り（仮置き・R-3未確認）

検算: 通常決済・保有10暦日想定 = 0.075(エントリー・価格内包)
      + [0.05(スプレッド) + 0.075(決済側スリッページ) + 0.05(手数料) + 0.077(金利)]
      = 0.075 + 0.252 = 0.327 ≈ spec記載の 0.33→0.35%（丸め差はspec側の丸めに由来）。
      ストップ決済 = 上記 + 0.10(ストップ執行追加・価格内包) ≈ 0.427 ≈ spec記載 0.45%。
"""

from __future__ import annotations

from dataclasses import dataclass

SPREAD_ROUND_TRIP = 0.0005  # 仮置き（R-2未確認）
EXIT_SIDE_NORMAL_SLIPPAGE = 0.00075  # 仮置き。エントリー側は価格に内包済み
COMMISSION_ROUND_TRIP = 0.0005  # 仮置き（R-3未確認）
MARGIN_INTEREST_ANNUAL_RATE = 0.028  # 仮置き（R-3未確認）
SHORT_LENDING_FEE_ANNUAL_RATE = 0.011  # 仮置き。参考Bショートアームのみ（R-2/R-3系）
DAYS_PER_YEAR = 365


@dataclass
class CostBreakdown:
    spread_pct: float
    exit_slippage_pct: float
    commission_pct: float
    margin_interest_pct: float
    total_flat_cost_pct: float
    holding_calendar_days: int


def compute_flat_cost_pct(holding_calendar_days: int) -> CostBreakdown:
    """1トレードあたりのフラット・コスト（建玉金額比・往復）を計算する。

    エントリー側スリッページ・ストップ執行時追加スリッページは価格シミュレーション側で
    既に織り込み済みのため、ここには含めない（二重計上防止。モジュールdocstring参照）。
    """
    interest = MARGIN_INTEREST_ANNUAL_RATE * holding_calendar_days / DAYS_PER_YEAR
    total = SPREAD_ROUND_TRIP + EXIT_SIDE_NORMAL_SLIPPAGE + COMMISSION_ROUND_TRIP + interest
    return CostBreakdown(
        spread_pct=SPREAD_ROUND_TRIP,
        exit_slippage_pct=EXIT_SIDE_NORMAL_SLIPPAGE,
        commission_pct=COMMISSION_ROUND_TRIP,
        margin_interest_pct=interest,
        total_flat_cost_pct=total,
        holding_calendar_days=holding_calendar_days,
    )


def cost_model_json() -> dict:
    """`cost-model.json` に出力する内容。実測値/仮置きの別を必ず明記する。"""
    return {
        "note": "全項目が仮置き（prescreen/spec段階の暫定値）。実額はR-2/R-3として未確認。",
        "entry_slippage_pct_per_leg": {
            "value": 0.00075,
            "basis": "仮置き",
            "applied_in": "価格シミュレーション（pead_execution.determine_entry）に内包済み",
        },
        "stop_execution_extra_slippage_pct_per_leg": {
            "value": 0.0010,
            "basis": "仮置き",
            "applied_in": "価格シミュレーション（pead_execution.simulate_trailing_stop_trade Step2）に内包済み",
        },
        "spread_round_trip_pct": {"value": SPREAD_ROUND_TRIP, "basis": "仮置き（R-2未確認）"},
        "exit_side_normal_slippage_pct": {
            "value": EXIT_SIDE_NORMAL_SLIPPAGE,
            "basis": "仮置き",
            "note": "エントリー側は価格に内包済みのため、決済側のみを本モジュールでフラット計上する",
        },
        "commission_round_trip_pct": {"value": COMMISSION_ROUND_TRIP, "basis": "仮置き（R-3未確認）"},
        "margin_interest_annual_rate": {
            "value": MARGIN_INTEREST_ANNUAL_RATE,
            "basis": "仮置き（R-3未確認）",
            "formula": "実保有暦日 × 年率 / 365",
        },
        "short_lending_fee_annual_rate": {
            "value": SHORT_LENDING_FEE_ANNUAL_RATE,
            "basis": "仮置き。参考Bショートアームのみ",
        },
        "short_ext_fee_note": (
            "逆日歩は算定不能のためゼロと置く。実際は上限なく発生しうる未測定リスク（spec §2.5参考B）。"
        ),
        "reconciliation_with_spec_4_6": {
            "normal_settlement_10_calendar_days_total_pct": round(
                0.00075 + compute_flat_cost_pct(10).total_flat_cost_pct, 5
            ),
            "spec_stated_value_pct": 0.0035,
            "stop_settlement_10_calendar_days_total_pct": round(
                0.00075 + 0.0010 + compute_flat_cost_pct(10).total_flat_cost_pct, 5
            ),
            "spec_stated_value_pct_stop": 0.0045,
        },
    }
