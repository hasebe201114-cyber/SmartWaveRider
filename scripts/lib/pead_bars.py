#!/usr/bin/env python3
"""EXP-OBS000001 日足バー取得の共通ロジック。

`scripts/check_daily_bars_coverage.py`（R-1d継続確認）と
`scripts/exp_obs000001_pead.py`（フル実装）の両方から使う、
`/equities/bars/daily` のレンジ取得・スナップショット取得の実処理。
クエリ方式の検出自体は `lib.jquants_client.detect_bars_daily_strategy` を使う。
"""

from __future__ import annotations

import datetime as dt

from lib.jquants_client import FetchOutcome, JQuantsClient


def weekdays_in_range(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def fetch_bars_range_by_code(
    client: JQuantsClient, codes: list[str], start: str, end: str, log: list[str]
) -> dict[str, list[dict]]:
    """`code`+`from`+`to` でレンジ取得できる場合の方式。1コード=1コール。"""
    bars: dict[str, list[dict]] = {}
    for i, code in enumerate(codes, 1):
        print(f"  [RANGE_BY_CODE] {i}/{len(codes)}: code={code}", flush=True)
        result = client.get("/equities/bars/daily", {"code": code, "from": start, "to": end})
        if "pagination_key" in (result.body or {}) or "paginationKey" in (result.body or {}):
            raise RuntimeError(
                f"code={code} の応答にページネーションキーが含まれる。"
                "ページング方式が未確認のため、部分データのまま前進しない。"
                f"レスポンスキー: {list(result.body.keys())}"
            )
        recs: list[dict] = []
        if result.body:
            for v in result.body.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    recs = v
                    break
        bars[code] = recs
        if result.outcome == FetchOutcome.RATE_LIMITED:
            log.append(f"- code={code}: RATE_LIMITED（429・リトライ後も解消せず。データ欠損ではない）")
        elif result.outcome == FetchOutcome.OK_EMPTY:
            log.append(f"- code={code}: OK_EMPTY（HTTP200・実データ0件）")
        elif result.outcome != FetchOutcome.OK_DATA:
            log.append(f"- code={code}: {result.outcome.value}（status={result.status}）")
    return bars


def fetch_bars_snapshot_by_date(
    client: JQuantsClient, codes: set[str], start: dt.date, end: dt.date, log: list[str]
) -> dict[str, list[dict]]:
    """`date` のみで全銘柄1日分が返る場合の方式。1営業日=1コール。"""
    bars: dict[str, list[dict]] = {c: [] for c in codes}
    days = list(weekdays_in_range(start, end))
    for i, d in enumerate(days, 1):
        print(f"  [SNAPSHOT_BY_DATE] {i}/{len(days)}: date={d.isoformat()}", flush=True)
        result = client.get("/equities/bars/daily", {"date": d.isoformat()})
        if result.outcome == FetchOutcome.RATE_LIMITED:
            log.append(f"- {d.isoformat()}: RATE_LIMITED（429・リトライ後も解消せず。データ欠損ではない）")
            continue
        if result.outcome == FetchOutcome.OK_EMPTY:
            continue  # 休日等（データ欠損ではなく正当な非営業日の可能性）
        if result.outcome != FetchOutcome.OK_DATA:
            log.append(f"- {d.isoformat()}: {result.outcome.value}（status={result.status}）")
            continue
        recs: list[dict] = []
        for v in result.body.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                recs = v
                break
        for r in recs:
            c = str(r.get("Code", ""))
            if c in bars:
                bars[c].append(r)
    return bars


def build_market_calendar(bars_by_code: dict[str, list[dict]]) -> list[str]:
    """観測されたバーの日付の和集合＝実測ベースの市場カレンダー（外部の祝日表に頼らない）。"""
    dates: set[str] = set()
    for recs in bars_by_code.values():
        for r in recs:
            if r.get("Date"):
                dates.add(str(r["Date"]))
    return sorted(dates)


def sort_bars_by_date(bars_by_code: dict[str, list[dict]]) -> dict[str, list[dict]]:
    return {code: sorted(recs, key=lambda r: str(r.get("Date", ""))) for code, recs in bars_by_code.items()}
