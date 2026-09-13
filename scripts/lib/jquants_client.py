"""J-Quants API V2 の共通クライアント（EXP-OBS000001 / PEAD 実装用）。

前回セッションで得た技術的教訓（司令塔指示）を反映する:
  - V2（APIキー方式・`x-api-key` ヘッダー）のみを使う（V1は2026-06-01廃止済み）
  - HTTP 200 でも中身が空リストのことがあるため、ステータスコードだけで成功判定しない
  - レート制限（429）に対しては指数バックオフでリトライする
  - APIキーは環境変数 `JQUANTS_API_KEY` から読む（.env.local は絶対に読まない）
  - 認証情報の値は一切ログに出さない

このモジュールは責務を「HTTP 通信」だけに絞る。ビジネスロジック（SUE定義・約定モデル等）は
呼び出し側（pead_*.py）に置く。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://api.jquants.com/v2"

# 基本間隔: 5秒（司令塔指示の「間隔5秒」を既定値とする）
MIN_REQUEST_INTERVAL_SEC = 5.0
# 429 時: 指数バックオフ（10s, 20s, 40s, 80s, 160s）。5回まで粘り、
# それでも429ならエラーとして記録する（真の欠測と混同しないため、呼び出し側で区別できるよう例外を投げる）。
RATE_LIMIT_BASE_BACKOFF_SEC = 10.0
RATE_LIMIT_MAX_RETRIES = 5


class RateLimitExhaustedError(RuntimeError):
    """429 が指数バックオフの上限まで続いた場合。真の欠測と混同しないよう専用の例外にする。"""


class JQuantsClient:
    def __init__(
        self,
        api_key: str | None = None,
        min_interval: float = MIN_REQUEST_INTERVAL_SEC,
        max_retries: int = RATE_LIMIT_MAX_RETRIES,
        base_backoff: float = RATE_LIMIT_BASE_BACKOFF_SEC,
        log_fn=None,
    ) -> None:
        self.api_key = api_key or os.environ.get("JQUANTS_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "JQUANTS_API_KEY が環境変数に見つからない。.env.local は読まない方針のため、"
                "環境変数として設定されていることを確認すること。"
            )
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self._last_request_at = 0.0
        self._ctx = ssl.create_default_context()
        self.log_fn = log_fn or (lambda msg: None)
        self.request_count = 0
        self.retry_count = 0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _do_request(self, url: str) -> tuple[int, dict | None, str]:
        req = urllib.request.Request(url, method="GET")
        req.add_header("x-api-key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=60, context=self._ctx) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(body), ""
                except json.JSONDecodeError:
                    return resp.status, None, "レスポンスがJSONではない"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            try:
                detail = json.loads(detail).get("message", detail)
            except Exception:
                pass
            return e.code, None, detail
        except urllib.error.URLError as e:
            return 0, None, f"接続失敗: {e.reason}"
        except Exception as e:  # noqa: BLE001
            return 0, None, f"例外: {type(e).__name__}: {e}"

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET を1回実行する。429は指数バックオフでリトライ。

        戻り値: レスポンスのJSON辞書（{"data": [...]})。
        - 200 以外（429リトライ枯渇後、401/403/400等）は例外を投げる（呼び出し側で捕捉し記録すること）。
        """
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{BASE_URL}{path}{qs}"
        self._throttle()
        self.request_count += 1
        status, body, err = self._do_request(url)
        backoff = self.base_backoff
        retries = 0
        while status == 429 and retries < self.max_retries:
            self.retry_count += 1
            self.log_fn(
                f"HTTP 429 (レート制限): {path} - {backoff:.0f}秒待機後リトライ "
                f"({retries + 1}/{self.max_retries})"
            )
            time.sleep(backoff)
            backoff *= 2.0  # 指数バックオフ
            self._throttle()
            status, body, err = self._do_request(url)
            retries += 1
        if status == 429:
            raise RateLimitExhaustedError(
                f"{path}: 429が指数バックオフ上限（{self.max_retries}回）まで解消しなかった"
            )
        if status != 200:
            raise RuntimeError(f"{path}: HTTP {status} - {err}")
        if body is None:
            raise RuntimeError(f"{path}: HTTP 200だがJSONの解析に失敗した")
        return body

    @staticmethod
    def has_real_data(body: dict | None) -> bool:
        """HTTP 200 でも中身が空リストのことがあるため、実データの有無を確認する。"""
        return bool(body) and any(isinstance(v, list) and v for v in body.values())


def business_days(start: "__import__('datetime').date", end: "__import__('datetime').date"):
    """start〜end（両端含む）の平日（月-金）を列挙する。祝日は考慮しない（API側が空応答を返すのみ）。"""
    import datetime as dt

    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
