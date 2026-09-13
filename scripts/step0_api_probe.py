#!/usr/bin/env python3
"""STEP0 API 実機疎通プローブ（タスク 0-8 / 0-9）

目的:
  - 0-9: J-Quants API で「何年分の・どの粒度のデータが・どれだけの遅延で」取れるかを実測する。
         この結果が重大論点 C-4（1時間足/15分足の過去データが無償で入手できない疑い）を決着させる。
  - 0-8: kabu STATION API（Windows 常駐アプリのローカルAPI）の疎通可否を確認する。

セキュリティ方針（CLAUDE.md 準拠）:
  - 認証情報は .env.local からこのスクリプトが読む。**値は一切出力しない**。
  - トークン・パスワード・メールアドレスはマスクして扱い、レポートにも残さない。
  - 生成されるレポートはそのまま共有・コミットしてよい内容のみを含む。

使い方:
    python3 scripts/step0_api_probe.py

    依存パッケージ不要（Python 3.11+ 標準ライブラリのみ）。

.env.local に必要な変数（いずれかの組み合わせ）:
    # 方式A: メールアドレス + パスワード
    JQUANTS_MAILADDRESS=...
    JQUANTS_PASSWORD=...

    # 方式B: リフレッシュトークン
    JQUANTS_REFRESH_TOKEN=...

    # 方式C: 既存の .env.example に合わせた名前でも可（リフレッシュトークンとして解釈を試みる）
    JQUANTS_API_KEY=...

    # 0-8 を試す場合（任意）
    KABU_API_PASSWORD=...
    KABU_API_MODE=sandbox   # sandbox(18081) / production(18080)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env.local"
REPORT_PATH = REPO_ROOT / "research" / "STEP0-api-probe-report.md"

JQUANTS_BASE = "https://api.jquants.com/v1"
TIMEOUT = 30

# 1単元が概ね20万円前後で、判断2（TOPIX500）にも含まれる代表銘柄をプローブ対象にする
PROBE_SYMBOLS = ["7203", "9984", "8306"]

# 提供開始時期を粗く二分するための年初営業日（土日祝は避けた平日）
YEAR_PROBE_DATES = [
    "2015-01-05", "2016-01-04", "2017-01-04", "2018-01-04", "2019-01-04",
    "2020-01-06", "2021-01-04", "2022-01-04", "2023-01-04", "2024-01-04",
    "2025-01-06", "2026-01-05",
]

# 粒度の実測対象。分足系のエンドポイントが存在しないことの確認が C-4 の核心。
ENDPOINT_PROBES = [
    ("/listed/info", {}, "上場銘柄一覧（ユニバース構築の土台）"),
    ("/prices/daily_quotes", {"code": "7203", "date": "2024-06-03"}, "日足 四本値"),
    ("/prices/prices_am", {}, "前場四本値（当日前場のみ・粒度は日中1本）"),
    ("/fins/statements", {"code": "7203"}, "財務諸表（PEAD のサプライズ度算出に使う）"),
    ("/fins/announcement", {}, "決算発表予定日（H-3 の決算跨ぎ回避に必須）"),
    ("/markets/trades_spec", {"from": "2024-06-01", "to": "2024-06-30"}, "投資部門別売買状況（需給スリーブ）"),
    ("/markets/weekly_margin_interest", {"code": "7203"}, "週次信用残（需給スリーブ）"),
    ("/markets/short_selling", {"sector33code": "0050"}, "業種別空売り比率"),
    ("/markets/breakdown", {"code": "7203", "date": "2024-06-03"}, "売買内訳データ"),
    ("/indices/topix", {}, "TOPIX 指数"),
]


def mask(value: str) -> str:
    """値そのものは絶対に出さない。長さだけ報告する。"""
    if not value:
        return "(未設定)"
    return f"(設定あり・{len(value)}文字・値は非表示)"


def load_env_local() -> dict[str, str]:
    """.env.local を読み込む。値は呼び出し側でもマスクして扱うこと。"""
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        env[key.strip()] = val
    return env


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | None, str]:
    """戻り値: (ステータスコード, JSON辞書 or None, エラー要約)"""
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


def get_id_token(env: dict[str, str], log: list[str]) -> str | None:
    """認証して idToken を得る。トークンは戻り値としてのみ扱い、出力しない。"""
    mail = env.get("JQUANTS_MAILADDRESS") or env.get("JQUANTS_MAIL_ADDRESS") or ""
    password = env.get("JQUANTS_PASSWORD", "")
    refresh = (
        env.get("JQUANTS_REFRESH_TOKEN")
        or env.get("JQUANTS_REFRESHTOKEN")
        or env.get("JQUANTS_API_KEY")
        or ""
    )

    log.append("### 認証情報の検出状況\n")
    log.append(f"- `JQUANTS_MAILADDRESS`: {mask(mail)}")
    log.append(f"- `JQUANTS_PASSWORD`: {mask(password)}")
    log.append(f"- リフレッシュトークン系: {mask(refresh)}")
    log.append("")

    if mail and password:
        status, body, err = http_json(
            f"{JQUANTS_BASE}/token/auth_user",
            method="POST",
            payload={"mailaddress": mail, "password": password},
        )
        if status == 200 and body and "refreshToken" in body:
            refresh = body["refreshToken"]
            log.append("- メールアドレス+パスワードでの認証: **成功**")
        else:
            log.append(f"- メールアドレス+パスワードでの認証: **失敗**（HTTP {status} / {err}）")

    if not refresh:
        log.append("- リフレッシュトークンが得られず、認証を中断した")
        return None

    status, body, err = http_json(
        f"{JQUANTS_BASE}/token/auth_refresh?refreshtoken={urllib.parse.quote(refresh)}",
        method="POST",
    )
    if status == 200 and body and "idToken" in body:
        log.append("- idToken の取得: **成功**")
        log.append("")
        return body["idToken"]

    log.append(f"- idToken の取得: **失敗**（HTTP {status} / {err}）")
    log.append("")
    return None


def probe_endpoints(token: str, log: list[str]) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    log.append("### 1. エンドポイント別のアクセス可否（＝契約プランで何が使えるか）\n")
    log.append("| エンドポイント | 用途 | 結果 |")
    log.append("|---|---|---|")
    for path, params, purpose in ENDPOINT_PROBES:
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        status, body, err = http_json(f"{JQUANTS_BASE}{path}{qs}", headers=headers)
        if status == 200:
            n = 0
            if body:
                for v in body.values():
                    if isinstance(v, list):
                        n = len(v)
                        break
            verdict = f"✅ 利用可（{n}件）" if n else "⚠️ 利用可だがデータ0件"
        elif status in (401, 403):
            verdict = f"🚫 プラン制限または権限なし（HTTP {status}）"
        elif status == 400:
            verdict = f"⚠️ パラメータ要調整（HTTP 400: {err[:60]}）"
        else:
            verdict = f"❌ HTTP {status} {err[:60]}"
        log.append(f"| `{path}` | {purpose} | {verdict} |")
    log.append("")


def probe_history_range(token: str, log: list[str]) -> None:
    """日足がどこまで遡れるか＝選定/確認分割が成立するかを実測する。"""
    headers = {"Authorization": f"Bearer {token}"}
    log.append("### 2. 日足の遡及可能範囲（PJ000001 §6.2 の選定/確認分割が成立するか）\n")
    log.append("| 日付 | データ有無 |")
    log.append("|---|---|")
    oldest_ok = None
    for date in YEAR_PROBE_DATES:
        found = False
        for sym in PROBE_SYMBOLS[:1]:
            status, body, _ = http_json(
                f"{JQUANTS_BASE}/prices/daily_quotes?code={sym}&date={date}",
                headers=headers,
            )
            if status == 200 and body and body.get("daily_quotes"):
                found = True
                break
        log.append(f"| {date} | {'✅ あり' if found else '— なし'} |")
        if found and oldest_ok is None:
            oldest_ok = date
    log.append("")
    if oldest_ok:
        log.append(f"**取得できた最も古い日付: {oldest_ok}**")
        if oldest_ok <= "2015-01-05":
            log.append("→ 選定期間 2015-2022 / 確認期間 2023-2026 の分割は**成立する**。")
        elif oldest_ok < "2023-01-01":
            log.append(
                f"→ 選定期間は {oldest_ok} 開始に短縮される。"
                "確認期間 2023-2026 は確保できるため分割自体は成立する。"
            )
        else:
            log.append(
                "→ **選定期間が確保できない。**"
                "PJ000001 §6.2 の分割を見直すか、有料プランを検討する必要がある。"
            )
    else:
        log.append("**いずれの日付でもデータを取得できなかった。**")
    log.append("")


def probe_delay(token: str, log: list[str]) -> None:
    """最新データがいつのものか＝遅延の実測。無料プランは12週間遅延とされる。"""
    headers = {"Authorization": f"Bearer {token}"}
    log.append("### 3. データ遅延の実測（無料プランは12週間遅延とされる）\n")
    today = dt.date.today()
    latest = None
    for back in range(0, 210, 7):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        status, body, _ = http_json(
            f"{JQUANTS_BASE}/prices/daily_quotes?code={PROBE_SYMBOLS[0]}&date={d.isoformat()}",
            headers=headers,
        )
        if status == 200 and body and body.get("daily_quotes"):
            latest = d
            break
    if latest:
        delay = (today - latest).days
        log.append(f"- 実行日: {today.isoformat()}")
        log.append(f"- 取得できた最新の日付: **{latest.isoformat()}**")
        log.append(f"- 遅延: **約 {delay} 日（{delay / 7:.1f} 週）**")
        if delay > 30:
            log.append(
                "- → この遅延では**ライブ運用および STEP3 フォワード較正に使えない**。"
                "バックテスト専用と割り切るか、有料プランが必要。"
            )
    else:
        log.append("- 直近210日以内に取得できるデータが見つからなかった。")
    log.append("")


def report_intraday_finding(log: list[str]) -> None:
    """C-4 の核心。分足エンドポイントの有無を明示的に記録する。"""
    log.append("### 4. 分足・時間足データの有無（重大論点 C-4 の決着材料）\n")
    log.append(
        "上記「1. エンドポイント別のアクセス可否」に、"
        "**1時間足・15分足に相当する時系列を返すエンドポイントが存在するか**を確認すること。"
    )
    log.append("")
    log.append("- `/prices/daily_quotes` は**日足**であり、日中の値動きは含まない")
    log.append(
        "- `/prices/prices_am` は**当日前場の四本値1本**であり、"
        "過去の分足時系列ではない（前場全体を1本に集約したもの）"
    )
    log.append("")
    log.append(
        "**判定**: 上記以外に分足系エンドポイントが見つからない場合、"
        "司令塔判断7（テクニカル系＝主軸1時間足・エントリー15分足）は "
        "**J-Quants からの過去データでは検証できない**ことが確定する。"
    )
    log.append("")
    log.append("その場合の選択肢（PJ000001 §4 C-4）:")
    log.append("1. 分足データを有料で調達する")
    log.append("2. テクニカル系スリーブを自前蓄積後のフォワード中心の検証に切り替える")
    log.append("3. 日足で代理検証してから分足へ移す")
    log.append("")
    log.append("※ 非価格系スリーブ（PEAD・需給）は日足で完結するため、この制約の影響を受けない。")
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
    log: list[str] = []
    log.append("# STEP0 API 実機疎通レポート（タスク 0-8 / 0-9）\n")
    log.append(f"- 実行日時: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.append(f"- 実行環境の Python: {sys.version.split()[0]}")
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

    log.append("## 0-9: J-Quants API 疎通\n")
    token = get_id_token(env, log)
    if token:
        probe_endpoints(token, log)
        probe_history_range(token, log)
        probe_delay(token, log)
        report_intraday_finding(log)
    else:
        log.append("認証に失敗したため、以降のプローブは実施できなかった。")
        log.append("`.env.local` の変数名が想定と一致しているか確認すること。")
        log.append("")

    probe_kabu(env, log)

    log.append("---\n")
    log.append("## 次のアクション\n")
    log.append("1. 本レポートを `research/STEP0-api-probe-report.md` としてコミットする")
    log.append("2. 「2. 日足の遡及可能範囲」と「4. 分足・時間足データの有無」の結果をもとに、")
    log.append("   `research/ACTIVE.md` のタスク 0-9 を完了、0-13（C-4）を決着させる")
    log.append("3. C-4 の選択肢1〜3のどれを採るかは司令塔判断とする")
    log.append("")

    out = "\n".join(log)
    print(out)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(out, encoding="utf-8")
    print(f"\n--- レポートを書き出しました: {REPORT_PATH} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
