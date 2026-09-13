#!/usr/bin/env python3
"""J-Quants API V2 共通クライアント（EXP-OBS000001 用）。

既存の `scripts/step0_api_probe.py` が確立した流儀（`.env.local` から
`JQUANTS_API_KEY` を読む・リクエスト間隔5秒・429時は10秒待って最大3回リトライ・
認証情報は一切ログに出さない）を土台にし、以下を追加する。

- **429（レート制限）とデータ欠損（200だが空）を明確に区別**して分類する
  （`FetchOutcome`）。前セッションの教訓（「全25点データなし」の実態が429だった）
  を再発させないための中核機能。
- **ローカルディスクキャッシュ**（`data/cache/jquants_v2/`）。再実行時に同じ
  クエリをAPIへ再送しない（spec §9.2「再現性」）。ただし **429・5xx・通信断は
  キャッシュしない**（一時的な失敗を「確定した結果」として固定してしまわないため）。
- **`/equities/bars/daily` のクエリ方式（レンジ取得が可能か）は未確認**であるため、
  推測で全銘柄×全日付を1件ずつ叩く（175銘柄×約500営業日=8万件超）前に、
  安価なプローブで「コード指定のみでレンジが返るか」「日付指定のみで全銘柄が
  返るか」を検出し、**どちらも確認できない場合は憶測で全件走査に入らず、
  明示的なフラグ（`--force-exhaustive`）なしには停止する**設計にしている
  （EXP-OBS000037 の教訓＝算術的な矛盾や未確認の前提を自己解釈で埋めない、を
  API呼び出し設計にも適用したもの）。

このモジュール自体は API を叩かない（インポート時に副作用なし）。
実行はすべて `scripts/check_daily_bars_coverage.py` /
`scripts/exp_obs000001_pead.py` から行う。

セキュリティ方針（CLAUDE.md 準拠）: 認証情報の値は一切出力・キャッシュしない。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# 既存資産（step0_api_probe.py）から再利用する。ロジックを重複させない。
from step0_api_probe import (  # noqa: E402
    ENV_FILE,
    JQUANTS_V2_BASE,
    MIN_REQUEST_INTERVAL_SEC,
    RATE_LIMIT_BACKOFF_SEC,
    RATE_LIMIT_MAX_RETRIES,
    debug_body_summary,
    get_api_key,
    has_real_data,
    http_json,
    load_env_local,
    mask,
    parse_subscription_range,
    v2_headers,
)

__all__ = [
    "ENV_FILE",
    "JQUANTS_V2_BASE",
    "MIN_REQUEST_INTERVAL_SEC",
    "RATE_LIMIT_BACKOFF_SEC",
    "RATE_LIMIT_MAX_RETRIES",
    "load_env_local",
    "get_api_key",
    "mask",
    "FetchOutcome",
    "FetchResult",
    "JQuantsClient",
    "BarsDailyStrategy",
    "detect_bars_daily_strategy",
    "UnconfirmedApiBehaviorError",
    "UniverseDefinitionError",
]

DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache" / "jquants_v2"


class FetchOutcome(str, Enum):
    """1回の API 呼び出しの分類結果。429 とデータ欠損を必ず区別する。"""

    OK_DATA = "ok_data"                        # HTTP 200・実データあり
    OK_EMPTY = "ok_empty"                      # HTTP 200・実データなし（休日・未上場等）
    OUT_OF_SUBSCRIPTION = "out_of_subscription"  # HTTP 400・契約範囲外（決定的）
    RATE_LIMITED = "rate_limited"              # HTTP 429（リトライ上限まで試して尚429）
    AUTH_ERROR = "auth_error"                  # HTTP 401/403
    HTTP_ERROR = "http_error"                  # その他の HTTP エラー
    TRANSPORT_ERROR = "transport_error"        # 通信断・タイムアウト等（status=0）

    @property
    def is_definitive(self) -> bool:
        """再現性のある確定結果か（キャッシュしてよいか）。429/通信断/未分類エラーは含めない。"""
        return self in (
            FetchOutcome.OK_DATA,
            FetchOutcome.OK_EMPTY,
            FetchOutcome.OUT_OF_SUBSCRIPTION,
        )

    @property
    def is_data_absence(self) -> bool:
        """『データが無い』と確定的に言えるか（429による見かけ上の欠損ではない）。"""
        return self in (FetchOutcome.OK_EMPTY, FetchOutcome.OUT_OF_SUBSCRIPTION)


@dataclass
class FetchResult:
    outcome: FetchOutcome
    status: int
    body: dict | None
    err: str
    from_cache: bool
    path: str
    params: dict[str, Any] = field(default_factory=dict)


class UnconfirmedApiBehaviorError(RuntimeError):
    """API のクエリ方式・レスポンス構造が実データで未確認のまま前進しようとしたときに送出する。

    自己解釈で埋めず、実測結果を添えて例外にすることで「憶測で進めない」を強制する。
    """


class UniverseDefinitionError(RuntimeError):
    """`ScaleCat` / `Mrgn` 等、ユニバース定義に必須のラベルが実データで確認できないときに送出する。

    spec §2.2:「想定と異なるラベル体系だった場合、独自判断で埋めずに
    S戦略チームへ差し戻す」（EXP-OBS000037 の教訓）を実装したもの。
    """


def _classify(status: int, body: dict | None, err: str) -> FetchOutcome:
    if status == 200:
        return FetchOutcome.OK_DATA if has_real_data(body) else FetchOutcome.OK_EMPTY
    if status == 400 and parse_subscription_range(err):
        return FetchOutcome.OUT_OF_SUBSCRIPTION
    if status == 429:
        return FetchOutcome.RATE_LIMITED
    if status in (401, 403):
        return FetchOutcome.AUTH_ERROR
    if status == 0:
        return FetchOutcome.TRANSPORT_ERROR
    return FetchOutcome.HTTP_ERROR


def _cache_key(path: str, params: dict[str, Any]) -> str:
    qs = urllib.parse.urlencode(sorted(params.items()))
    digest = hashlib.sha1(f"{path}?{qs}".encode("utf-8")).hexdigest()
    safe_path = path.strip("/").replace("/", "_")
    return f"{safe_path}__{digest}.json"


class JQuantsClient:
    """スロットリング・リトライ・キャッシュ・429判別を1箇所にまとめたクライアント。"""

    def __init__(
        self,
        api_key: str,
        *,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        use_cache: bool = True,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self._api_key = api_key
        self._cache_dir = cache_dir
        self._use_cache = use_cache
        self._log = log_fn
        self.request_count = 0
        self.cache_hit_count = 0
        self.outcome_counts: dict[FetchOutcome, int] = {o: 0 for o in FetchOutcome}
        if self._use_cache:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, path: str, params: dict[str, Any]) -> Path:
        return self._cache_dir / _cache_key(path, params)

    def _read_cache(self, path: str, params: dict[str, Any]) -> FetchResult | None:
        if not self._use_cache:
            return None
        cache_path = self._cache_path(path, params)
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        outcome = FetchOutcome(payload["outcome"])
        return FetchResult(
            outcome=outcome,
            status=payload["status"],
            body=payload["body"],
            err=payload.get("err", ""),
            from_cache=True,
            path=path,
            params=params,
        )

    def _write_cache(self, result: FetchResult) -> None:
        if not self._use_cache or not result.outcome.is_definitive:
            return
        cache_path = self._cache_path(result.path, result.params)
        payload = {
            "path": result.path,
            "params": result.params,
            "status": result.status,
            "body": result.body,
            "err": result.err,
            "cached_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=None), encoding="utf-8"
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> FetchResult:
        """1エンドポイントを1回呼ぶ。キャッシュ確認→無ければ実際に叩く→分類→キャッシュ。"""
        params = params or {}
        cached = self._read_cache(path, params)
        if cached is not None:
            self.cache_hit_count += 1
            self.outcome_counts[cached.outcome] += 1
            return cached

        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{JQUANTS_V2_BASE}{path}{qs}"
        status, body, err = http_json(url, headers=v2_headers(self._api_key))
        self.request_count += 1
        outcome = _classify(status, body, err)
        result = FetchResult(
            outcome=outcome,
            status=status,
            body=body,
            err=err,
            from_cache=False,
            path=path,
            params=params,
        )
        self.outcome_counts[outcome] += 1
        self._write_cache(result)

        if outcome == FetchOutcome.RATE_LIMITED:
            self._log(
                f"[RATE_LIMITED] {path} params={params} -> HTTP 429 "
                f"({RATE_LIMIT_MAX_RETRIES}回リトライ後も解消せず。データ欠損ではない）"
            )
        elif outcome == FetchOutcome.TRANSPORT_ERROR:
            self._log(f"[TRANSPORT_ERROR] {path} params={params} -> {err}")
        elif outcome == FetchOutcome.HTTP_ERROR:
            self._log(f"[HTTP_ERROR] {path} params={params} -> HTTP {status} {err[:120]}")
        elif outcome == FetchOutcome.AUTH_ERROR:
            self._log(f"[AUTH_ERROR] {path} params={params} -> HTTP {status} {err[:120]}")
        return result

    def summary(self) -> dict[str, int]:
        return {
            "requests_sent": self.request_count,
            "cache_hits": self.cache_hit_count,
            **{f"outcome_{o.value}": c for o, c in self.outcome_counts.items()},
        }


class BarsDailyStrategy(str, Enum):
    """`/equities/bars/daily` の効率的な取得方式（実測で検出する。憶測で決め打ちしない）。"""

    RANGE_BY_CODE = "range_by_code"      # code + from/to で1コードの全期間が返る
    SNAPSHOT_BY_DATE = "snapshot_by_date"  # date のみで全銘柄1日分が返る
    PAIR_ONLY = "pair_only"              # code+date の組でしか1件も返らない（確認済み・低効率）


def _count_distinct_dates(body: dict | None) -> int:
    if not body:
        return 0
    dates: set[str] = set()
    for v in body.values():
        if isinstance(v, list):
            for rec in v:
                if isinstance(rec, dict) and "Date" in rec:
                    dates.add(str(rec["Date"]))
    return len(dates)


def _count_distinct_codes(body: dict | None) -> int:
    if not body:
        return 0
    codes: set[str] = set()
    for v in body.values():
        if isinstance(v, list):
            for rec in v:
                if isinstance(rec, dict) and "Code" in rec:
                    codes.add(str(rec["Code"]))
    return len(codes)


def detect_bars_daily_strategy(
    client: JQuantsClient,
    *,
    probe_code: str,
    probe_date: str,
    range_from: str,
    range_to: str,
    log: list[str],
) -> BarsDailyStrategy:
    """`/equities/bars/daily` のクエリ方式を安価な数回のリクエストで検出する。

    確認済みなのは `code`+`date` の組で1件返ることのみ（既存プローブの実測）。
    それ以外（`code` のみ・`from`/`to` 併用・`date` のみ）は未確認の仮説であり、
    ここで実測してから使う方式を決める。**いずれも確認できない場合は
    `PAIR_ONLY` を返し、呼び出し側で全件走査の是非を明示的に判断させる。**
    """
    log.append("### `/equities/bars/daily` のクエリ方式検出（未確認の前提を実測で確認）\n")

    # 仮説A: code のみ（日付省略）でレンジが返るか
    r_code_only = client.get("/equities/bars/daily", {"code": probe_code})
    n_dates_a = _count_distinct_dates(r_code_only.body)
    log.append(
        f"- 仮説A `code={probe_code}`（date省略）: outcome={r_code_only.outcome.value} "
        f"status={r_code_only.status} distinct_dates={n_dates_a} "
        f"body={debug_body_summary(r_code_only.body)}"
    )

    # 仮説B: code + from/to でレンジが返るか
    r_range = client.get(
        "/equities/bars/daily", {"code": probe_code, "from": range_from, "to": range_to}
    )
    n_dates_b = _count_distinct_dates(r_range.body)
    log.append(
        f"- 仮説B `code={probe_code}&from={range_from}&to={range_to}`: "
        f"outcome={r_range.outcome.value} status={r_range.status} distinct_dates={n_dates_b} "
        f"body={debug_body_summary(r_range.body)}"
    )

    # 仮説C: date のみ（code省略）で全銘柄1日分が返るか
    r_date_only = client.get("/equities/bars/daily", {"date": probe_date})
    n_codes_c = _count_distinct_codes(r_date_only.body)
    log.append(
        f"- 仮説C `date={probe_date}`（code省略）: outcome={r_date_only.outcome.value} "
        f"status={r_date_only.status} distinct_codes={n_codes_c} "
        f"body={debug_body_summary(r_date_only.body)}"
    )
    log.append("")

    if n_dates_b > n_dates_a and n_dates_b > 1:
        log.append(f"→ **RANGE_BY_CODE を採用**（`from`/`to` 併用で {n_dates_b} 日分を1コールで取得できた）")
        log.append("")
        return BarsDailyStrategy.RANGE_BY_CODE
    if n_dates_a > 1:
        log.append(f"→ **RANGE_BY_CODE を採用**（`code` のみで {n_dates_a} 日分を1コールで取得できた）")
        log.append("")
        return BarsDailyStrategy.RANGE_BY_CODE
    if n_codes_c > 1:
        log.append(f"→ **SNAPSHOT_BY_DATE を採用**（`date` のみで {n_codes_c} 銘柄分を1コールで取得できた）")
        log.append("")
        return BarsDailyStrategy.SNAPSHOT_BY_DATE

    log.append(
        "→ **いずれの仮説も確認できなかった（PAIR_ONLY）**。"
        "`/equities/bars/daily` は `code` と `date` の組でしか1件を返さない可能性が高い。"
        "175銘柄×約500営業日の全件走査は現行の間隔設定（5秒/リクエスト）で "
        "8万件超×5秒 ≈ 4.6日を要し、単一セッションでの実行に適さない。"
        "本スクリプトはここで停止する。`--force-exhaustive` を明示指定した場合のみ、"
        "件数を絞った代替案（例: 月内複数日サンプリング）を検討すること。"
        "**この挙動は spec に書かれていない前提であるため、効率的な取得方式が実在するかを"
        "J-Quants の API リファレンス（マイページ）で確認し、必要ならS戦略チームへ相談すること。**"
    )
    log.append("")
    return BarsDailyStrategy.PAIR_ONLY
