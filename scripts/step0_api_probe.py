#!/usr/bin/env python3
"""STEP0 API 実機疎通プローブ（タスク 0-8 / 0-9）

目的:
  - 0-9: J-Quants API で「何年分の・どの粒度のデータが・どれだけの遅延で」取れるかを実測する。
         この結果が重大論点 C-4（1時間足/15分足の過去データが無償で入手できない疑い）を決着させる。
  - 0-8: kabu STATION API（Windows 常駐アプリのローカルAPI）の疎通可否を確認する。

重要な前提（2026-09-13 に判明）:
  J-Quants API は V1 が 2026-06-01 に廃止されており、現在は **V2（APIキー方式）のみ**が有効。
  V1（メールアドレス+パスワード → リフレッシュトークン → IDトークン、ベースURL api.jquants.com/v1）
  を前提にした本スクリプトの旧版は、廃止済みエンドポイントを叩いて 403 になっていた。
  V2 は `x-api-key` ヘッダーに直接 APIキーを載せる方式で、ベースURLは api.jquants.com/v2。

  V2 の正確なエンドポイントパス一覧は一次情報源（公式サイト）を自動取得できず確認できていない
  （Bot対策で 403）。判明しているのは `/equities/bars/daily` のみ。分足・時間足を含む他のパスは
  複数の**推測候補**を実測して仕分ける方式にしている。的中しなくても 404 として記録されるだけで
  安全であり、どれかが 200 を返せばそれが正しいパスだと分かる。

セキュリティ方針（CLAUDE.md 準拠）:
  - 認証情報は .env.local からこのスクリプトが読む。**値は一切出力しない**。
  - トークン・パスワード・APIキーはマスクして扱い、レポートにも残さない。
  - 生成されるレポートはそのまま共有・コミットしてよい内容のみを含む。

使い方:
    python scripts/step0_api_probe.py

    依存パッケージ不要（Python 3.11+ 標準ライブラリのみ）。

.env.local に必要な変数:
    JQUANTS_API_KEY=...     # J-Quants マイページ（ダッシュボード）で発行した V2 用 APIキー

    # 0-8 を試す場合（任意）
    KABU_API_PASSWORD=...
    KABU_API_MODE=sandbox   # sandbox(18081) / production(18080)
"""

from __future__ import annotations

import datetime as dt
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env.local"
REPORT_PATH = REPO_ROOT / "research" / "STEP0-api-probe-report.md"

JQUANTS_V2_BASE = "https://api.jquants.com/v2"
TIMEOUT = 30
# 前回の実測で HTTP 429 (レート制限) が多発した。全リクエストの間に最低これだけ間隔を空ける。
MIN_REQUEST_INTERVAL_SEC = 0.6
# 429 を受けたときのリトライ回数と待機秒数（指数バックオフ）
RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_BACKOFF_BASE_SEC = 3.0

# 1単元が概ね20万円前後で、判断2（TOPIX500）にも含まれる代表銘柄をプローブ対象にする
PROBE_CODE = "72030"  # トヨタ自動車（V2 は5桁コード表記の可能性があるため後段で両対応を試す）
PROBE_CODE_ALT = "7203"

# 確認済み・推測を含むエンドポイント候補。
# 確認済みは "/equities/bars/daily" のみ。他は公式ドキュメントを直接取得できなかったため、
# 命名規則から類推した複数候補を並べ、実測でどれが有効か（200）を判定する。
# 日付パラメータは "{DATE}" / "{DATE_FROM}" / "{DATE_TO}" というプレースホルダにしておき、
# 実行時に「契約範囲内で確認できた有効な日付」へ差し替える（固定日付だと契約範囲外で
# 400 になり、無関係なエンドポイントまで誤って「利用不可」と判定してしまうため）。
ENDPOINT_PROBES: list[tuple[str, dict, str, bool]] = [
    # (パス, パラメータ, 用途, 確認済みか)
    ("/equities/master", {}, "上場銘柄一覧（ユニバース構築の土台）", False),
    ("/equities/bars/daily", {"code": "{CODE}", "date": "{DATE}"}, "日足 四本値【確認済みパス】", True),
    ("/fins/summary", {"code": "{CODE}"}, "財務情報（PEAD のサプライズ度算出に使う）", False),
    ("/equities/earnings-calendar", {}, "決算発表予定（H-3 の決算跨ぎ回避に必須）", False),
    ("/indices/bars/daily", {"code": "0000"}, "TOPIX等 指数 日足", False),
    ("/markets/trading-by-type", {"from": "{DATE_FROM}", "to": "{DATE_TO}"}, "投資部門別売買状況（需給スリーブ）", False),
    ("/markets/margin-interest", {"code": "{CODE}"}, "信用残（需給スリーブ）", False),
    ("/markets/short-selling", {}, "業種別空売り比率", False),
]

# C-4 の核心: 分足・時間足に相当しそうなパスを推測で総当たりする。
# 1つでも 200 を返せば、それが正式パスである可能性が高い。
INTRADAY_ENDPOINT_CANDIDATES: list[tuple[str, dict]] = [
    ("/equities/bars/minute", {"code": "{CODE}", "date": "{DATE}"}),
    ("/equities/bars/intraday", {"code": "{CODE}", "date": "{DATE}"}),
    ("/equities/bars/1m", {"code": "{CODE}", "date": "{DATE}"}),
    ("/equities/bars/hourly", {"code": "{CODE}", "date": "{DATE}"}),
    ("/equities/bars/am", {"code": "{CODE}", "date": "{DATE}"}),
    ("/equities/prices/am", {"code": "{CODE}", "date": "{DATE}"}),
    ("/equities/prices/minute", {"code": "{CODE}", "date": "{DATE}"}),
]


def resolve_params(params: dict, valid_date: str, valid_code: str) -> dict:
    """{DATE} 等のプレースホルダを、実行時に判明した有効な値へ差し替える。"""
    resolved = {}
    for k, v in params.items():
        if v == "{DATE}":
            resolved[k] = valid_date
        elif v == "{DATE_FROM}":
            d = dt.date.fromisoformat(valid_date) - dt.timedelta(days=30)
            resolved[k] = d.isoformat()
        elif v == "{DATE_TO}":
            resolved[k] = valid_date
        elif v == "{CODE}":
            resolved[k] = valid_code
        else:
            resolved[k] = v
    return resolved


def mask(value: str) -> str:
    """値そのものは絶対に出さない。長さだけ報告する。"""
    if not value:
        return "(未設定)"
    return f"(設定あり・{len(value)}文字・値は非表示)"


def read_text_tolerant(path: Path) -> str:
    """Windows で保存された設定ファイルを想定し、文字コードを順に試す。

    メモ帳等は BOM 付き UTF-8 や cp932(Shift_JIS) で保存することがあるため、
    UTF-8 決め打ちだと読み込みに失敗する。
    """
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    # どれでも読めない場合は文字化けを許容してでも変数名だけは拾う
    return path.read_text(encoding="utf-8", errors="replace")


def load_env_local() -> dict[str, str]:
    """.env.local を読み込む。値は呼び出し側でもマスクして扱うこと。"""
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for raw in read_text_tolerant(ENV_FILE).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        env[key.strip()] = val
    return env


_last_request_at: float = 0.0


def _throttle() -> None:
    """全リクエストに最低限の間隔を強制する（レート制限対策）。"""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_SEC:
        time.sleep(MIN_REQUEST_INTERVAL_SEC - elapsed)
    _last_request_at = time.monotonic()


def _http_json_once(
    url: str, *, method: str, payload: dict | None, headers: dict | None
) -> tuple[int, dict | None, str]:
    """1回だけ叩く。戻り値: (ステータスコード, JSON辞書 or None, エラー要約)"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body), ""
            except json.JSONDecodeError:
                return resp.status, None, "レスポンスが JSON ではない"
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        # エラー本文に認証情報が混ざる可能性を避けるため、message フィールドのみ拾う
        try:
            detail = json.loads(detail).get("message", detail)
        except Exception:
            pass
        return e.code, None, detail
    except urllib.error.URLError as e:
        return 0, None, f"接続失敗: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return 0, None, f"例外: {type(e).__name__}"


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | None, str]:
    """戻り値: (ステータスコード, JSON辞書 or None, エラー要約)

    前回の実測で HTTP 429（レート制限）が多発し、大半のプローブが「データなし」と
    誤判定される原因になった。全リクエストに最低間隔を強制し、429 が返った場合は
    指数バックオフで自動リトライする。
    """
    _throttle()
    status, body, err = _http_json_once(url, method=method, payload=payload, headers=headers)
    retries = 0
    while status == 429 and retries < RATE_LIMIT_MAX_RETRIES:
        wait = RATE_LIMIT_BACKOFF_BASE_SEC * (2**retries)
        time.sleep(wait)
        retries += 1
        _throttle()
        status, body, err = _http_json_once(url, method=method, payload=payload, headers=headers)
    return status, body, err


def has_real_data(body: dict | None) -> bool:
    """HTTP 200 でも中身が空リストのことがあるため、実データの有無を確認する。"""
    return bool(body) and any(isinstance(v, list) and v for v in body.values())


def debug_body_summary(body: dict | None, max_keys: int = 6) -> str:
    """レスポンスの構造を1行で要約する（値そのものは出さず、キー名・型・件数のみ）。

    R-1（会社予想フィールドの有無）や、200なのにデータが空という矛盾の原因調査に使う。
    """
    if not body:
        return "(空またはJSON以外)"
    parts = []
    for k, v in list(body.items())[:max_keys]:
        if isinstance(v, list):
            if v and isinstance(v[0], dict):
                inner_keys = ",".join(list(v[0].keys())[:10])
                parts.append(f"{k}=list[{len(v)}]先頭要素keys=({inner_keys})")
            else:
                parts.append(f"{k}=list[{len(v)}]")
        elif isinstance(v, dict):
            parts.append(f"{k}=dict keys=({','.join(list(v.keys())[:10])})")
        else:
            parts.append(f"{k}={type(v).__name__}")
    more = "" if len(body) <= max_keys else f" ...他{len(body) - max_keys}キー"
    return "; ".join(parts) + more


def get_api_key(env: dict[str, str], log: list[str]) -> str | None:
    """V2 のAPIキーを .env.local から取得する。値は出力しない。"""
    api_key = (
        env.get("JQUANTS_API_KEY")
        or env.get("JQUANTS_APIKEY")
        or env.get("JQUANTS_REFRESH_TOKEN")  # 旧設定名との後方互換
        or ""
    )
    log.append("### 認証情報の検出状況\n")
    log.append(f"- `JQUANTS_API_KEY`: {mask(env.get('JQUANTS_API_KEY', ''))}")
    if not env.get("JQUANTS_API_KEY") and api_key:
        log.append("- （`JQUANTS_API_KEY` が空だったため、別名の変数を代わりに使用した）")
    log.append("")
    if not api_key:
        log.append("- APIキーが見つからず、疎通を中断した。`.env.local` に `JQUANTS_API_KEY` を設定すること。")
        log.append("")
        return None
    return api_key


def v2_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


SUBSCRIPTION_RANGE_RE = re.compile(
    r"covers the following dates:\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})"
)


def parse_subscription_range(message: str) -> tuple[dt.date, dt.date] | None:
    """HTTP 400 のエラーメッセージから契約がカバーする日付範囲を抽出する。

    実例: "Your subscription covers the following dates: 2024-06-21 ~ 2026-06-21.
           If you want more data, please check other plans"
    これは「APIキーが無効」ではなく「クエリした日付が契約範囲外」という正常なレスポンス。
    """
    m = SUBSCRIPTION_RANGE_RE.search(message)
    if not m:
        return None
    start = dt.date.fromisoformat(m.group(1))
    end = dt.date.fromisoformat(m.group(2))
    return start, end


def sanity_check(
    api_key: str, log: list[str]
) -> tuple[bool, dt.date | None, dt.date | None, str | None, str]:
    """まず軽いエンドポイントでキー自体が有効かを確認する。

    戻り値: (成功したか, 契約開始日, 契約終了日, 疎通に使えた実際の日付(ISO文字列), 有効な銘柄コード)
    HTTP 400 で「契約範囲外」と言われた場合はキー自体は有効なので、
    契約範囲内の日付で取り直して成功させる。
    HTTP 200 でも中身が空リストのことがあるため、実データの有無まで確認し、
    5桁コード（推測）が空なら4桁コードにフォールバックする。
    """
    log.append("### 0. APIキーの有効性チェック\n")
    today = dt.date.today().isoformat()
    status, body, err = http_json(
        f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE}&date={today}",
        headers=v2_headers(api_key),
    )
    if status == 200:
        real = has_real_data(body)
        log.append(f"- `/equities/bars/daily`（{today}）への疎通: HTTP 200")
        log.append(f"- レスポンス構造: `{debug_body_summary(body)}`")
        if real:
            log.append("- **実データを含む200**: 疎通確認完了")
            log.append("")
            return True, None, None, today, PROBE_CODE
        log.append(
            f"- ⚠️ HTTP 200だが実データが空。銘柄コード `{PROBE_CODE}`（5桁推測表記）が"
            f"誤っている可能性があるため、4桁表記 `{PROBE_CODE_ALT}` で再試行する"
        )
        status_alt, body_alt, _ = http_json(
            f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE_ALT}&date={today}",
            headers=v2_headers(api_key),
        )
        if status_alt == 200 and has_real_data(body_alt):
            log.append(f"- 4桁表記 `{PROBE_CODE_ALT}` で再試行: **成功**（HTTP 200・実データあり）")
            log.append("")
            return True, None, None, today, PROBE_CODE_ALT
        log.append(f"- 4桁表記でも実データなし（レスポンス: `{debug_body_summary(body_alt)}`）")
        log.append("")

    if status == 400:
        rng = parse_subscription_range(err)
        if rng:
            start, end = rng
            log.append(
                f"- `/equities/bars/daily`（{today}）への疎通: HTTP 400"
                f"（**APIキーは有効**。クエリ日付が契約範囲外だっただけ）"
            )
            log.append(f"- **契約がカバーする日付範囲: {start.isoformat()} 〜 {end.isoformat()}**")
            if end < dt.date.today():
                log.append(
                    f"- ⚠️ 契約終了日（{end.isoformat()}）が実行日（{dt.date.today().isoformat()}）より過去。"
                    "契約期間が既に終了しているか、期限が固定された過去ログ用プランの可能性がある。"
                )
            log.append("")
            # 契約範囲内（終了日の直前の平日）で再テストして本当に取得できるか確認する
            probe_date = end
            while probe_date.weekday() >= 5:
                probe_date -= dt.timedelta(days=1)
            status2, body2, err2 = http_json(
                f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE}&date={probe_date.isoformat()}",
                headers=v2_headers(api_key),
            )
            real2 = status2 == 200 and has_real_data(body2)
            log.append(f"- 契約範囲内の日付（{probe_date.isoformat()}）で再テスト: HTTP {status2}")
            log.append(f"- レスポンス構造: `{debug_body_summary(body2)}`")
            if real2:
                log.append("- **実データあり**")
                log.append("")
                return True, start, end, probe_date.isoformat(), PROBE_CODE
            # 5桁がダメなら4桁でも試す
            status3, body3, err3 = http_json(
                f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE_ALT}&date={probe_date.isoformat()}",
                headers=v2_headers(api_key),
            )
            real3 = status3 == 200 and has_real_data(body3)
            if real3:
                log.append(
                    f"- 4桁表記 `{PROBE_CODE_ALT}` で再試行: **成功**（HTTP 200・実データあり）。"
                    "以降のプローブも4桁表記に切り替える"
                )
                log.append("")
                return True, start, end, probe_date.isoformat(), PROBE_CODE_ALT
            log.append(
                f"- 4桁表記でも実データなし（レスポンス: `{debug_body_summary(body3)}`）。"
                f"HTTPステータスは200でも実データが得られていない: **失敗**"
            )
            log.append("")
            return False, start, end, None, PROBE_CODE

    if status in (401, 403):
        log.append(f"- `/equities/bars/daily`（{today}）への疎通: **失敗**（HTTP {status} / {err[:120]}）")
        log.append(
            "- → APIキー自体が無効、期限切れ、またはプラン未契約の可能性が高い。"
            "J-Quants マイページでキーの状態・契約プランを確認すること。"
        )
        log.append("")
        return False, None, None, None, PROBE_CODE

    log.append(f"- `/equities/bars/daily`（{today}）への疎通: **予期しない結果**（HTTP {status} / {err[:120]}）")
    log.append("")
    return False, None, None, None, PROBE_CODE


def probe_endpoints(api_key: str, valid_date: str, valid_code: str, log: list[str]) -> None:
    headers = v2_headers(api_key)
    log.append("### 1. エンドポイント別のアクセス可否（＝契約プランで何が使えるか）\n")
    log.append(f"（日付パラメータは契約範囲内で疎通確認済みの `{valid_date}`、銘柄コードは `{valid_code}` を使用）\n")
    log.append("| エンドポイント | 用途 | 結果 |")
    log.append("|---|---|---|")
    for path, raw_params, purpose, confirmed in ENDPOINT_PROBES:
        params = resolve_params(raw_params, valid_date, valid_code)
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        status, body, err = http_json(f"{JQUANTS_V2_BASE}{path}{qs}", headers=headers)
        tag = "" if confirmed else "（推測パス）"
        if status == 200:
            n = 0
            if body:
                for v in body.values():
                    if isinstance(v, list):
                        n = len(v)
                        break
            if n > 0:
                verdict = f"✅ 利用可（{n}件）"
            else:
                verdict = f"⚠️ HTTP 200だが0件（`{debug_body_summary(body)}`）"
        elif status == 404:
            verdict = f"❓ パス不明（HTTP 404。推測パスが外れている可能性）" if not confirmed else "❌ HTTP 404（確認済みパスのはずが404。要再確認）"
        elif status in (401, 403):
            verdict = f"🚫 プラン制限または権限なし（HTTP {status}）"
        elif status == 400:
            verdict = f"⚠️ パラメータ要調整（HTTP 400: {err[:60]}）"
        else:
            verdict = f"❌ HTTP {status} {err[:60]}"
        log.append(f"| `{path}`{tag} | {purpose} | {verdict} |")
    log.append("")


def probe_intraday(api_key: str, valid_date: str, valid_code: str, log: list[str]) -> None:
    """C-4 の核心。分足・時間足エンドポイントの候補を総当たりする。"""
    headers = v2_headers(api_key)
    log.append("### 2. 分足・時間足データの有無（重大論点 C-4 の決着材料）\n")
    log.append(
        "以下は**推測パスの総当たり**である。1つでも 200（かつ実データあり）が返れば、"
        "それが正式な分足エンドポイントである可能性が高い。"
        "全滅した場合、少なくとも本スクリプトが試した範囲では分足の提供を確認できなかったことを意味する"
        "（正式パスがまだ特定できていない可能性は残る）。\n"
    )
    log.append("| エンドポイント（推測） | 結果 |")
    log.append("|---|---|")
    any_hit = False
    for path, raw_params in INTRADAY_ENDPOINT_CANDIDATES:
        params = resolve_params(raw_params, valid_date, valid_code)
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        status, body, err = http_json(f"{JQUANTS_V2_BASE}{path}{qs}", headers=headers)
        if status == 200 and has_real_data(body):
            verdict = "✅ **200 成功・実データあり — 分足/時間足エンドポイントの可能性大**"
            any_hit = True
        elif status == 200:
            verdict = f"⚠️ HTTP 200だが実データなし（`{debug_body_summary(body)}`）"
        elif status == 404:
            verdict = "— 404（このパスは存在しない）"
        elif status in (401, 403):
            verdict = f"🚫 HTTP {status}（パスは存在するがプラン外の可能性）"
        else:
            verdict = f"❌ HTTP {status} {err[:60]}"
        log.append(f"| `{path}` | {verdict} |")
    log.append("")
    if any_hit:
        log.append("**→ 200 を返したパスがある。C-4 は「入手可能」の方向で再判定が必要。**")
    else:
        log.append(
            "**→ 推測した範囲では分足/時間足エンドポイントを発見できなかった。**"
            "ただし本スクリプトのパス推測が外れているだけの可能性があるため、"
            "J-Quants マイページの API リファレンス（ログイン後に閲覧可能）で"
            "分足関連エンドポイントの掲載有無を目視確認することを推奨する。"
        )
    log.append("")


def month_starts_in_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """start〜end の範囲内で、月初め直近の平日を列挙する（実測を間引くため）。"""
    dates: list[dt.date] = [start]
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        d = cur
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        if start <= d <= end and d not in dates:
            dates.append(d)
        cur = dt.date(cur.year + 1, 1, 1) if cur.month == 12 else dt.date(cur.year, cur.month + 1, 1)
    return sorted(set(dates))


def probe_history_range(
    api_key: str,
    subscription_start: dt.date | None,
    subscription_end: dt.date | None,
    valid_code: str,
    log: list[str],
) -> None:
    """日足がどこまで遡れるか＝選定/確認分割が成立するかを実測する。

    0. APIキーの有効性チェックで判明した「契約がカバーする日付範囲」を土台にし、
       その範囲内で実際にデータが返るかを月次で実測する。
    """
    headers = v2_headers(api_key)
    log.append("### 3. 日足の遡及可能範囲（PJ000001 §6.2 の選定/確認分割が成立するか）\n")

    if not subscription_start or not subscription_end:
        log.append(
            "契約範囲が特定できなかったため、本項の実測は省略した"
            "（「0. APIキーの有効性チェック」の結果を参照）。"
        )
        log.append("")
        return

    log.append(
        f"契約がカバーする日付範囲: **{subscription_start.isoformat()} 〜 {subscription_end.isoformat()}**"
        f"（0番の結果より）。銘柄コードは `{valid_code}`（0番で実データが確認できたもの）を使用し、"
        "この範囲内で月次に実測する。\n"
    )
    log.append("| 日付 | データ有無 |")
    log.append("|---|---|")
    oldest_ok = None
    debug_shown = 0
    for date in month_starts_in_range(subscription_start, subscription_end):
        status, body, _ = http_json(
            f"{JQUANTS_V2_BASE}/equities/bars/daily?code={valid_code}&date={date.isoformat()}",
            headers=headers,
        )
        found = status == 200 and has_real_data(body)
        if found:
            log.append(f"| {date.isoformat()} | ✅ あり |")
            if oldest_ok is None:
                oldest_ok = date
        else:
            # 最初の数件だけ、なぜ「なし」なのかの生の手がかりを残す（デバッグ用）
            if debug_shown < 3:
                log.append(f"| {date.isoformat()} | — なし（HTTP {status}・`{debug_body_summary(body)}`） |")
                debug_shown += 1
            else:
                log.append(f"| {date.isoformat()} | — なし |")
    log.append("")

    oldest_iso = oldest_ok.isoformat() if oldest_ok else subscription_start.isoformat()
    log.append(f"**実測できた最も古い日付: {oldest_iso}**")
    if oldest_iso <= "2015-01-05":
        log.append("→ 選定期間 2015-2022 / 確認期間 2023-2026 の分割は**成立する**。")
    else:
        log.append(
            f"→ **選定期間 2015-2022 は確保できない**（契約は {subscription_start.isoformat()} までしか遡れない）。"
            "PJ000001 §6.2 の選定/確認分割は、この契約の下では成立しない。"
            "確認期間のみのフォワード中心の検証、または有料プランの検討が必要。"
        )
    log.append("")


def probe_delay(
    api_key: str, subscription_end: dt.date | None, valid_code: str, log: list[str]
) -> None:
    """最新データがいつのものか＝遅延の実測。無料プランは12週間遅延とされる。"""
    headers = v2_headers(api_key)
    log.append("### 4. データ遅延の実測（無料プランは12週間遅延とされる）\n")
    today = dt.date.today()
    search_from = subscription_end if subscription_end else today
    latest = None
    for back in range(0, 210):
        d = search_from - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        status, body, _ = http_json(
            f"{JQUANTS_V2_BASE}/equities/bars/daily?code={valid_code}&date={d.isoformat()}",
            headers=headers,
        )
        if status == 200 and has_real_data(body):
            latest = d
            break
    if latest:
        delay = (today - latest).days
        log.append(f"- 実行日: {today.isoformat()}")
        log.append(f"- 取得できた最新の日付: **{latest.isoformat()}**")
        log.append(f"- 実行日との差: **約 {delay} 日（{delay / 7:.1f} 週）**")
        if subscription_end and subscription_end < today:
            log.append(
                f"- この差は「配信遅延」ではなく、**契約終了日（{subscription_end.isoformat()}）が"
                "実行日より過去であること**が主因の可能性が高い。契約の更新・プラン確認が必要。"
            )
        elif delay > 30:
            log.append(
                "- → この遅延では**ライブ運用および STEP3 フォワード較正に使えない**。"
                "バックテスト専用と割り切るか、有料プランが必要。"
            )
    else:
        log.append("- 探索範囲内に取得できるデータが見つからなかった。")
    log.append("")


def probe_fins_summary_schema(api_key: str, valid_code: str, log: list[str]) -> None:
    """R-1: /fins/summary に会社業績予想フィールドが含まれるか、キー一覧から目視判断できるようにする。

    PEAD prescreen（EXP-OBS000001）が「致命的」と位置づけた確認事項。
    会社予想フィールドが無ければ SUE（サプライズ度）が算出できず、PEAD の前提が崩れる。
    """
    headers = v2_headers(api_key)
    log.append("### 5. `/fins/summary` のフィールド構造（PEAD prescreen R-1: 会社業績予想の有無）\n")
    status, body, err = http_json(f"{JQUANTS_V2_BASE}/fins/summary?code={valid_code}", headers=headers)
    if status != 200 or not body:
        log.append(f"- 取得失敗（HTTP {status} / {err[:120]}）。R-1 は未確認のまま。")
        log.append("")
        return
    log.append(f"- レスポンス構造: `{debug_body_summary(body, max_keys=10)}`")
    # 中身のリストから1レコード分のキー一覧を全部出す（会社予想らしきフィールド名を目視できるように）
    record = None
    for v in body.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            record = v[0]
            break
    if record:
        log.append(f"- 1レコードの全フィールド名: `{', '.join(record.keys())}`")
        log.append(
            "- 上記に「Forecast」「予想」「会社予想」に相当するフィールド"
            "（例: ForecastNetSales, ForecastOperatingProfit 等）が含まれるか目視確認すること。"
            "含まれていれば R-1 は解消、含まれていなければ SUE が算出できず PEAD は成立しない"
        )
    else:
        log.append("- レコード本体を特定できなかった。レスポンス構造を目視確認すること。")
    log.append("")


def probe_master_schema(api_key: str, valid_code: str, log: list[str]) -> None:
    """R-1c: /equities/master に規模区分（TOPIX500構成銘柄識別用）フィールドがあるか確認する。"""
    headers = v2_headers(api_key)
    log.append("### 6. `/equities/master` のフィールド構造（PEAD prescreen R-1c: ユニバース定義）\n")
    status, body, err = http_json(f"{JQUANTS_V2_BASE}/equities/master?code={valid_code}", headers=headers)
    if status != 200 or not body:
        status, body, err = http_json(f"{JQUANTS_V2_BASE}/equities/master", headers=headers)
    if status != 200 or not body:
        log.append(f"- 取得失敗（HTTP {status} / {err[:120]}）。R-1c は未確認のまま。")
        log.append("")
        return
    log.append(f"- レスポンス構造: `{debug_body_summary(body, max_keys=10)}`")
    record = None
    for v in body.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            record = v[0]
            break
    if record:
        log.append(f"- 1レコードの全フィールド名: `{', '.join(record.keys())}`")
        log.append(
            "- 上記に「ScaleCategory」「規模区分」「TOPIX」に相当するフィールドがあるか目視確認すること。"
            "指数系エンドポイントが403で使えないため、ユニバース定義（TOPIX500構成銘柄の識別）は"
            "このフィールドの有無に懸かっている"
        )
    else:
        log.append("- レコード本体を特定できなかった。レスポンス構造を目視確認すること。")
    log.append("")


def probe_kabu(env: dict[str, str], log: list[str]) -> None:
    """0-8: kabu STATION API の疎通確認。Windows 常駐アプリが必要。"""
    log.append("## 0-8: kabu STATION API 疎通\n")
    mode = env.get("KABU_API_MODE", "sandbox").strip().lower()
    port = 18080 if mode == "production" else 18081
    log.append(f"- モード: `{mode}` → ポート **{port}**")

    reachable = False
    try:
        with socket.create_connection(("localhost", port), timeout=3):
            reachable = True
    except OSError as e:
        log.append(f"- localhost:{port} への接続: **失敗**（{type(e).__name__}）")

    if not reachable:
        log.append(
            "- → kabu STATION（Windows常駐アプリ）が起動していないか、"
            "API 利用設定が有効になっていない。"
        )
        log.append(
            "- **これは PJ000001 §4 H-2 が指摘した単一障害点そのもの**。"
            "常駐PCが落ちていれば取引も停止する。"
        )
        log.append("")
        return

    log.append(f"- localhost:{port} への接続: **成功**")
    password = env.get("KABU_API_PASSWORD", "")
    log.append(f"- `KABU_API_PASSWORD`: {mask(password)}")
    if not password:
        log.append("- パスワード未設定のためトークン発行は試行しない（接続確認のみ）")
        log.append("")
        return

    status, body, err = http_json(
        f"http://localhost:{port}/kabusapi/token",
        method="POST",
        payload={"APIPassword": password},
    )
    if status == 200 and body and body.get("Token"):
        log.append("- APIトークンの発行: **成功**（トークンは非表示）")
        log.append("- → 0-8 は疎通確認済みとしてよい")
    else:
        log.append(f"- APIトークンの発行: **失敗**（HTTP {status} / {err[:80]}）")
    log.append("")


def main() -> int:
    # Windows でコンソール出力をリダイレクトすると cp932 になり、
    # レポート中の絵文字（✅ 等）で UnicodeEncodeError になるため UTF-8 に固定する。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  古い環境では無視してよい
            pass

    log: list[str] = []
    log.append("# STEP0 API 実機疎通レポート（タスク 0-8 / 0-9）\n")
    log.append(f"- 実行日時: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.append(f"- 実行環境の Python: {sys.version.split()[0]}")
    log.append("- 対象: J-Quants API **V2**（V1 は 2026-06-01 廃止済みのため対象外）")
    log.append("- **本レポートに認証情報は一切含まれない**（値は長さのみ記録）")
    log.append("")

    env = load_env_local()
    if not env:
        log.append(
            f"> ⚠️ `{ENV_FILE}` が見つからないか空。"
            "認証情報を読み込めないため、疎通は実施できない。\n"
        )
        print("\n".join(log))
        REPORT_PATH.write_text("\n".join(log), encoding="utf-8")
        return 1

    log.append("## 0-9: J-Quants API (V2) 疎通\n")
    api_key = get_api_key(env, log)
    if api_key:
        ok, sub_start, sub_end, valid_date, valid_code = sanity_check(api_key, log)
        if ok and valid_date:
            probe_endpoints(api_key, valid_date, valid_code, log)
            probe_intraday(api_key, valid_date, valid_code, log)
            probe_history_range(api_key, sub_start, sub_end, valid_code, log)
            probe_delay(api_key, sub_end, valid_code, log)
            probe_fins_summary_schema(api_key, valid_code, log)
            probe_master_schema(api_key, valid_code, log)
        else:
            log.append("有効な日付が特定できなかったため、以降のプローブは実施しなかった。")
            log.append("J-Quants マイページでキーの発行状態・契約プランを確認すること。")
            log.append("")

    probe_kabu(env, log)

    log.append("---\n")
    log.append("## 次のアクション\n")
    log.append("1. 本レポートを `research/STEP0-api-probe-report.md` としてコミットする")
    log.append("2. 「3. 日足の遡及可能範囲」と「2. 分足・時間足データの有無」の結果をもとに、")
    log.append("   `research/ACTIVE.md` のタスク 0-9 を完了、0-13（C-4）を決着させる")
    log.append("3. C-4 の選択肢1〜3のどれを採るかは司令塔判断とする")
    log.append("4. 推測パスが的中しなかった場合、J-Quants マイページの API リファレンスで")
    log.append("   正式なエンドポイント名を目視確認し、本スクリプトの候補リストを更新する")
    log.append("")

    out = "\n".join(log)
    print(out)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(out, encoding="utf-8")
    print(f"\n--- レポートを書き出しました: {REPORT_PATH} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
