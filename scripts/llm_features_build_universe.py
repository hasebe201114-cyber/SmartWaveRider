#!/usr/bin/env python3
"""③LLM特徴量フォワード蓄積トラックの凍結ユニバース U-LLM を生成する。

定義（`research/_snapshots/llm_features/00-preregistration.md` §4 と同一）:
    U-LLM = EXP-OBS000001（PEAD）spec §5.4 の **U-1〜U-5 を通過した銘柄の全集合**
            （U-6 の「最大175銘柄」上限は適用しない）
            基準日 = 確認期間開始日 2025-07-01

実測値: 271銘柄（`research/EXP-OBS000001/10-result/universe.json` の
`confirmation_universe.main_band_pass_count` = 271 と一致することを検算する）。

本スクリプトは `data/raw/pead/` のキャッシュのみを読み、ネットワークに出ない。
同じキャッシュからは常に同じ271銘柄を返す（決定的）。

出力: `research/_snapshots/llm_features/frozen/universe_v1.json`
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.pead_common import MRGN_U2_OK, SCALECAT_U1_OK  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"
PEAD_RESULT = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"
OUT_PATH = REPO_ROOT / "research" / "_snapshots" / "llm_features" / "frozen" / "universe_v1.json"

AS_OF = dt.date(2025, 7, 1)  # PEAD 確認期間開始日
LIQUIDITY_MIN_VA = 5e8  # U-4
PRICE_BAND = (1000.0, 3500.0)  # U-5 主条件
VA_WINDOW = 60  # U-4 の窓（直前60営業日）


def _trailing_median_va(bars: list[dict], as_of: dt.date, window: int) -> float | None:
    prior = sorted(
        (r for r in bars if dt.date.fromisoformat(r["Date"]) < as_of), key=lambda r: r["Date"]
    )[-window:]
    vas = sorted(float(r["Va"]) for r in prior if r.get("Va") is not None)
    if not vas:
        return None
    n = len(vas)
    mid = n // 2
    return vas[mid] if n % 2 == 1 else (vas[mid - 1] + vas[mid]) / 2.0


def _price_at_or_before(bars: list[dict], as_of: dt.date) -> tuple[float | None, str | None]:
    eligible = sorted(
        (r for r in bars if dt.date.fromisoformat(r["Date"]) <= as_of), key=lambda r: r["Date"]
    )
    if not eligible:
        return None, None
    return float(eligible[-1]["C"]), eligible[-1]["Date"]


def build() -> dict:
    master = {
        r["Code"]: r
        for r in json.loads(
            (RAW_DIR / f"master_confirmation_start_{AS_OF.isoformat()}.json").read_text(encoding="utf-8")
        )["data"]
    }
    candidates = json.loads((PEAD_RESULT / "candidate_codes.json").read_text(encoding="utf-8"))["codes"]

    passed: list[dict] = []
    for code in candidates:
        m = master.get(code)
        if m is None:
            continue
        if m.get("ScaleCat") not in SCALECAT_U1_OK:  # U-1
            continue
        if m.get("Mrgn") not in MRGN_U2_OK:  # U-2
            continue
        bar_path = RAW_DIR / "bars_daily" / f"{code}.json"
        if not bar_path.exists():
            continue
        bars = json.loads(bar_path.read_text(encoding="utf-8"))
        va = _trailing_median_va(bars, AS_OF, VA_WINDOW)  # U-4
        if va is None or va < LIQUIDITY_MIN_VA:
            continue
        price, price_date = _price_at_or_before(bars, AS_OF)  # U-5
        if price is None or not (PRICE_BAND[0] <= price <= PRICE_BAND[1]):
            continue
        passed.append(
            {
                "code": code,
                "co_name": m.get("CoName"),
                "scale_cat": m.get("ScaleCat"),
                "s33": m.get("S33"),
                "s33_nm": m.get("S33Nm"),
                "va_median_60bd": va,
                "price": price,
                "price_date": price_date,
            }
        )

    passed.sort(key=lambda r: r["code"])
    codes = [r["code"] for r in passed]

    out = {
        "universe_id": "U-LLM-v1",
        "rule": "EXP-OBS000001 spec §5.4 U-1〜U-5 を通過した全銘柄（U-6の175上限は適用しない）",
        "as_of": AS_OF.isoformat(),
        "u4_window_business_days": VA_WINDOW,
        "u4_min_median_turnover_jpy": LIQUIDITY_MIN_VA,
        "u5_price_band_jpy": list(PRICE_BAND),
        "count": len(codes),
        "codes": codes,
        "codes_sha256": hashlib.sha256(json.dumps(codes, separators=(",", ":")).encode()).hexdigest(),
        "details": passed,
        "generated_from": "scripts/llm_features_build_universe.py",
        "deterministic": True,
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="既存ファイルと一致するかだけ検証し、書き込まない")
    args = ap.parse_args()

    out = build()

    # 検算: PEAD の universe.json の main_band_pass_count と一致するはず
    pead = json.loads((PEAD_RESULT / "universe.json").read_text(encoding="utf-8"))
    expected = pead["confirmation_universe"]["main_band_pass_count"]
    pead175 = set(pead["confirmation_universe"]["codes"])
    ok_count = out["count"] == expected
    ok_superset = pead175 <= set(out["codes"])
    print(f"U-LLM count={out['count']} (PEAD main_band_pass_count={expected}) -> {'OK' if ok_count else 'MISMATCH'}")
    print(f"PEAD 175銘柄が U-LLM の部分集合か -> {'OK' if ok_superset else 'MISMATCH'}")
    print(f"codes_sha256={out['codes_sha256']}")
    if not (ok_count and ok_superset):
        print("STOP: 検算不一致。凍結ユニバースを書き出さない。")
        return 2

    if args.check:
        if not OUT_PATH.exists():
            print("既存ファイルなし")
            return 1
        cur = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        same = cur.get("codes_sha256") == out["codes_sha256"]
        print(f"既存ファイルとの一致: {'OK' if same else 'MISMATCH'}")
        return 0 if same else 3

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
