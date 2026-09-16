#!/usr/bin/env python3
"""10Y-COMMON §2・§3・§5.1 の T（参照営業日カレンダー）と確定日集合 U_dates を機械的に構成する。

前提: `jq10y_fetch_data.py --step bars` が完了していること（`bars_by_date/*.json` のファイル名＝日付が T になる）。

出力: `data/raw/jq10y/calendar.json`
  - T: 昇順の日付リスト（1始まり添字は idx = 配列位置+1）
  - T1, T_len, T61, T68, split_date, s, T_s_minus_1, T_len_date
  - U_dates: 確定日集合（10Y-COMMON §5.1）

**推定日付をハードコードしない**（§1）。すべて実測データから機械的に算出する。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "jq10y"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    bars_dir = RAW_DIR / "bars_by_date"
    files = sorted(bars_dir.glob("*.json"))
    T = [fp.stem for fp in files]  # ファイル名 = 実際にデータが存在した日付（D-1: 1件以上の行がある日）
    if not T:
        log("エラー: bars_by_date にファイルが無い。先に --step bars を完了させること。")
        return 1

    n = len(T)
    T1 = T[0]
    T_len_date = T[-1]

    def at(i: int) -> str:
        """1始まり添字。"""
        return T[i - 1]

    T61 = at(61) if n >= 61 else None
    T68 = at(68) if n >= 68 else None

    # split_date := T の中で 2020-01-01 以降の最小の日付
    split_date = None
    s = None
    for i, d in enumerate(T, start=1):
        if d >= "2020-01-01":
            split_date = d
            s = i
            break
    if s is None:
        log("エラー: T に2020-01-01以降の日付が存在しない。")
        return 1
    T_s_minus_1 = at(s - 1)

    # U_dates（10Y-COMMON §5.1）: { T[61] } ∪ { 各暦年yの最初の営業日 : y in [2017, T[|T|]の年] }
    # ただし T[61] より前の日付は含めない。
    last_year = int(T_len_date[:4])
    year_first_days: dict[str, str] = {}
    for d in T:
        y = d[:4]
        if y not in year_first_days:
            year_first_days[y] = d
    u_dates_set = set()
    if T61 is not None:
        u_dates_set.add(T61)
    for y in range(2017, last_year + 1):
        yd = year_first_days.get(str(y))
        if yd is not None and (T61 is None or yd >= T61):
            u_dates_set.add(yd)
    U_dates = sorted(u_dates_set)

    calendar = {
        "T": T,
        "T_len": n,
        "T1": T1,
        "T_len_date": T_len_date,
        "T61_idx": 61 if n >= 61 else None,
        "T61": T61,
        "T68_idx": 68 if n >= 68 else None,
        "T68": T68,
        "split_date": split_date,
        "s_idx": s,
        "T_s_minus_1_idx": s - 1,
        "T_s_minus_1": T_s_minus_1,
        "selection_range": [T68, T_s_minus_1],
        "confirmation_range": [split_date, T_len_date],
        "U_dates": U_dates,
        "U_dates_count": len(U_dates),
        "methodology": (
            "T = data/raw/jq10y/bars_by_date/*.json のファイル名（=/equities/bars/daily に"
            "1件以上の行が存在した日付）を昇順に並べたもの。1始まり添字。"
            "U_dates = {T[61]} ∪ {各暦年の最初の営業日: y∈[2017, T[|T|]の年]}（T[61]より前を除く）。"
            "すべて実測データから機械的に算出し、推定日付はハードコードしていない（10Y-COMMON §1）。"
        ),
    }

    out_path = RAW_DIR / "calendar.json"
    out_path.write_text(json.dumps(calendar, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"T1={T1} T_len={n} T_len_date={T_len_date}")
    log(f"T61={T61} T68={T68} split_date={split_date} s={s} T[s-1]={T_s_minus_1}")
    log(f"selection_range={T68}〜{T_s_minus_1}  confirmation_range={split_date}〜{T_len_date}")
    log(f"U_dates ({len(U_dates)}): {U_dates}")
    log(f"saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
