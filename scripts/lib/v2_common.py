"""EXP-OBS000005（PEAD）/ EXP-OBS000006（ギャップ）共通ロジック（10年データ版・spec第2版）。

spec: `research/EXP-OBS000005/01-spec.md` §2.0.1 / `research/EXP-OBS000006/01-spec.md` §2.0.1。

第2版の要点:
  - `/equities/bars/daily`（`code` 指定のみ）はクエリ実行日から遡るローリング窓を返す
    （固定アーカイブではない）。B実装チームが §9-1 実行中に発見した事実。
  - `T` は契約範囲 `[2016-09-15, 2026-09-14]` で切り取って（ピン留めして）構成する（§2.0.1）。
  - 候補銘柄集合 `candidate_codes_v2.json`（554銘柄）は両EXPで共用。
  - **既存の `data/raw/pead/bars_daily/` を上書き・再取得・削除してはならない**（§9-0・N-10/N-15）。
    このモジュールはロード専用であり、書き込み・fetch は一切行わない。

`CalendarV2` は `gap_common.Calendar` と同一のインターフェース（`T` / `idx` / `at` / `row` /
`business_day_on_or_before` / `codes` / `bars_by_code` / `missing_bars_codes`）を提供するため、
`gap_engine.py` の既存関数（`build_valid_positions` / `sigma_gap` / `evaluate_candidate_row` /
`build_period_rows` / `build_period_universe` 等）をそのまま再利用できる。
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
from pathlib import Path

from . import gap_common as gc

REPO_ROOT = gc.REPO_ROOT
PEAD_RAW_DIR = gc.PEAD_RAW_DIR
PEAD_RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000005" / "10-result"
GAP_RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000006" / "10-result"

# spec §2.0.1（両EXP共通）。00-prescreen.md §A-1 が実測前（コミット b5080ee）に凍結した範囲。
CONTRACT_START = "2016-09-15"
CONTRACT_END = "2026-09-14"

# spec §9-1（両EXP共用）。00-prescreen.md §B-1 の実測値 T[61]・T[m+1] と一致する。
D_SEL_V2 = dt.date(2016, 12, 15)
D_CONF_V2 = dt.date(2021, 9, 16)

RAW_DATA_BACKUP_PATH = "/home/user/backups/data_raw_backup_20260915.tar.gz"


def load_candidate_codes_v2() -> list[str]:
    d = json.loads((PEAD_RESULT_DIR / "candidate_codes_v2.json").read_text(encoding="utf-8"))
    return d["codes"]


class CalendarV2:
    """spec §2.0.1: 契約範囲 `[2016-09-15, 2026-09-14]` で切り取った `T`。

    `gap_common.Calendar` と同一の読み取り専用インターフェースを提供する。書き込みは行わない
    （既存キャッシュの上書き禁止・N-10/N-15 を、実装上も「読むだけ」にすることで担保する）。
    """

    def __init__(self, codes: list[str]):
        self.codes = codes
        self.bars_by_code: dict[str, dict[str, dict]] = {}
        self.missing_bars_codes: list[str] = []
        # ローリング窓による先頭/末尾欠けの実測用（§9-9）。生データ（切り取り前）の初日・最終日。
        self.raw_first_date: dict[str, str] = {}
        self.raw_last_date: dict[str, str] = {}
        self.raw_row_count: dict[str, int] = {}
        date_set: set[str] = set()
        for code in codes:
            rows = gc.load_bars_raw(code)
            if rows is None:
                self.missing_bars_codes.append(code)
                continue
            by_date: dict[str, dict] = {}
            all_dates: list[str] = []
            for r in rows:
                d = r["Date"]
                all_dates.append(d)
                if CONTRACT_START <= d <= CONTRACT_END:
                    by_date[d] = r
                    date_set.add(d)
            self.bars_by_code[code] = by_date
            self.raw_row_count[code] = len(rows)
            if all_dates:
                self.raw_first_date[code] = min(all_dates)
                self.raw_last_date[code] = max(all_dates)
        self.T: list[str] = sorted(date_set)
        self._index = {d: i + 1 for i, d in enumerate(self.T)}

    def idx(self, date_str: str) -> int | None:
        return self._index.get(date_str)

    def at(self, i: int) -> str | None:
        if 1 <= i <= len(self.T):
            return self.T[i - 1]
        return None

    def row(self, code: str, date_str: str) -> dict | None:
        return self.bars_by_code.get(code, {}).get(date_str)

    def business_day_on_or_before(self, calendar_date: dt.date) -> str | None:
        s = calendar_date.isoformat()
        pos = bisect.bisect_right(self.T, s)
        if pos == 0:
            return None
        return self.T[pos - 1]


def verify_backup() -> dict:
    """§9-0: バックアップの実在確認（tar検証はしない。存在とサイズのみ確認する軽量チェック）。"""
    p = Path(RAW_DATA_BACKUP_PATH)
    return {
        "raw_data_backup_path": RAW_DATA_BACKUP_PATH,
        "backup_exists": p.exists(),
        "backup_size_bytes": p.stat().st_size if p.exists() else None,
    }
