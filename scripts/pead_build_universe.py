#!/usr/bin/env python3
"""EXP-OBS000001（PEAD）ユニバース構築（spec §5.4 U-1〜U-7）。

前提: `pead_fetch_data.py --step master` と `--step bars`（候補銘柄分）が完了していること。

出力:
  - `research/EXP-OBS000001/10-result/universe.json`
      選定期間・確認期間それぞれのユニバース（銘柄コード・適用条件・件数）
  - 9-1 の値対応付け結果は `pead_feasibility.py` 側で `params.json` にまとめて出力する
    （このスクリプトは中間データとして `10-result/field_value_mapping.json` を出す）

決定的である（同じキャッシュから同じ結果が出る）。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.pead_common import MRGN_U2_OK, SCALECAT_U1_OK  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"
RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"

SELECTION_START = dt.date(2024, 6, 21)
CONFIRMATION_START = dt.date(2025, 7, 1)

PRICE_BAND_MAIN = (1000.0, 3500.0)  # U-5 主条件
PRICE_BAND_FALLBACK = (700.0, 8000.0)  # U-7 緩和条件（株価帯のみ）
LIQUIDITY_MIN_VA = 5e8  # U-4: 5億円
UNIVERSE_MAX = 175  # U-6
UNIVERSE_MIN = 150  # U-7 判定基準


def load_master(label: str, date: dt.date) -> dict[str, dict]:
    path = RAW_DIR / f"master_{label}_{date.isoformat()}.json"
    data = json.loads(path.read_text(encoding="utf-8"))["data"]
    return {r["Code"]: r for r in data}


def load_bars(code: str) -> list[dict] | None:
    path = RAW_DIR / "bars_daily" / f"{code}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def trailing_median_va(bars: list[dict], as_of: dt.date, window: int = 60) -> float | None:
    """as_of より厳密に前の営業日（その銘柄の行が存在する日）を最大window件遡り、Vaの中央値を返す。"""
    prior = [r for r in bars if dt.date.fromisoformat(r["Date"]) < as_of]
    prior.sort(key=lambda r: r["Date"])
    window_rows = prior[-window:]
    if not window_rows:
        return None
    vas = sorted(float(r["Va"]) for r in window_rows if r.get("Va") is not None)
    if not vas:
        return None
    n = len(vas)
    mid = n // 2
    if n % 2 == 1:
        return vas[mid]
    return (vas[mid - 1] + vas[mid]) / 2.0


def price_at_or_before(bars: list[dict], as_of: dt.date) -> tuple[float | None, str | None]:
    """as_of以前の最新営業日のCを返す（as_ofが営業日ならその日、そうでなければ直近前営業日）。"""
    eligible = [r for r in bars if dt.date.fromisoformat(r["Date"]) <= as_of]
    if not eligible:
        return None, None
    eligible.sort(key=lambda r: r["Date"])
    last = eligible[-1]
    return float(last["C"]), last["Date"]


def build_period_universe(period_label: str, period_start: dt.date, candidate_codes: list[str], log: list[str]) -> dict:
    master = load_master(
        "selection_start" if period_label == "selection" else "confirmation_start", period_start
    )

    rows = []
    missing_bars = []
    for code in candidate_codes:
        m = master.get(code)
        if m is None:
            continue  # その時点でmasterに存在しない（上場前/廃止後等）
        u1 = m.get("ScaleCat") in SCALECAT_U1_OK
        u2 = m.get("Mrgn") in MRGN_U2_OK
        if not (u1 and u2):
            continue
        bars = load_bars(code)
        if bars is None:
            missing_bars.append(code)
            continue
        va_median = trailing_median_va(bars, period_start, window=60)
        price, price_date = price_at_or_before(bars, period_start)
        rows.append(
            {
                "code": code,
                "co_name": m.get("CoName"),
                "scale_cat": m.get("ScaleCat"),
                "mrgn": m.get("Mrgn"),
                "s33": m.get("S33"),
                "s33_nm": m.get("S33Nm"),
                "va_median_60bd": va_median,
                "price": price,
                "price_date": price_date,
            }
        )

    def apply_filters(price_band: tuple[float, float]) -> list[dict]:
        out = []
        for r in rows:
            if r["va_median_60bd"] is None or r["va_median_60bd"] < LIQUIDITY_MIN_VA:
                continue
            if r["price"] is None or not (price_band[0] <= r["price"] <= price_band[1]):
                continue
            out.append(r)
        return out

    passed_main = apply_filters(PRICE_BAND_MAIN)
    fallback_applied = False
    passed = passed_main
    if len(passed_main) < UNIVERSE_MIN:
        fallback_applied = True
        passed = apply_filters(PRICE_BAND_FALLBACK)

    unresolved = len(passed) < UNIVERSE_MIN
    truncated = False
    if len(passed) > UNIVERSE_MAX:
        passed = sorted(passed, key=lambda r: -r["va_median_60bd"])[:UNIVERSE_MAX]
        truncated = True

    log.append(
        f"[{period_label}] candidates={len(candidate_codes)} u1u2_pass={len(rows)} "
        f"missing_bars={len(missing_bars)} main_band_pass={len(passed_main)} "
        f"fallback_applied={fallback_applied} final_count={len(passed)} truncated={truncated} "
        f"unresolved(<150)={unresolved}"
    )

    return {
        "period": period_label,
        "period_start": period_start.isoformat(),
        "candidate_count": len(candidate_codes),
        "u1_u2_pass_count": len(rows),
        "missing_bars_codes": missing_bars,
        "price_band_main": PRICE_BAND_MAIN,
        "price_band_fallback_applied": fallback_applied,
        "price_band_fallback": PRICE_BAND_FALLBACK if fallback_applied else None,
        "main_band_pass_count": len(passed_main),
        "final_count": len(passed),
        "truncated_to_max": truncated,
        "unresolved_below_min": unresolved,
        "codes": [r["code"] for r in passed],
        "details": passed,
    }


def main() -> int:
    candidate_codes = json.loads((RESULT_DIR / "candidate_codes.json").read_text(encoding="utf-8"))["codes"]
    log: list[str] = []
    selection = build_period_universe("selection", SELECTION_START, candidate_codes, log)
    confirmation = build_period_universe("confirmation", CONFIRMATION_START, candidate_codes, log)

    out = {
        "generated_from": "pead_build_universe.py",
        "rule_reference": "spec §5.4 U-1〜U-7",
        "selection_universe": selection,
        "confirmation_universe": confirmation,
    }
    out_path = RESULT_DIR / "universe.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    for line in log:
        print(line)
    print(f"saved: {out_path}")

    if selection["unresolved_below_min"] or confirmation["unresolved_below_min"]:
        print(
            "STOP: U-7緩和後もユニバース成立条件（>=150銘柄）を満たさない期間がある。"
            "spec §5.4 U-7により S へ差し戻しが必要。"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
