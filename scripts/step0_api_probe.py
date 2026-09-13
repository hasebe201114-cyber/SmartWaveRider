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

JQUANTS_V2_BASE = "https://api.jquants.com/v2"
TIMEOUT = 30

# 1単元が概ね20万円前後で、判断2（TOPIX500）にも含まれる代表銘柄をプローブ対象にする
PROBE_CODE = "72030"  # トヨタ自動車（V2 は5桁コード表記の可能性があるため後段で両対応を試す）
PROBE_CODE_ALT = "7203"

# 提供開始時期を粗く二分するための年初営業日（土日祝は避けた平日）
YEAR_PROBE_DATES = [
    "2015-01-05", "2016-01-04", "2017-01-04", "2018-01-04", "2019-01-04",
    "2020-01-06", "2021-01-04", "2022-01-04", "2023-01-04", "2024-01-04",
    "2025-01-06", "2026-01-05",
]

# 確認済み・推測を含むエンドポイント候補。
# 確認済みは "/equities/bars/daily" のみ。他は公式ドキュメントを直接取得できなかったため、
# 命名規則から類推した複数候補を並べ、実測でどれが有効か（200）を判定する。
ENDPOINT_PROBES: list[tuple[str, dict, str, bool]] = [
    # (パス, パラメータ, 用途, 確認済みか)
    ("/equities/master", {}, "上場銘柄一覧（ユニバース構築の土台）", False),
    ("/equities/bars/daily", {"code": PROBE_CODE, "date": "2024-06-03"}, "日足 四本値【確認済みパス】", True),
    ("/fins/summary", {"code": PROBE_CODE}, "財務情報（PEAD のサプライズ度算出に使う）", False),
    ("/equities/earnings-calendar", {}, "決算発表予定（H-3 の決算跨ぎ回避に必須）", False),
    ("/indices/bars/daily", {"code": "0000"}, "TOPIX等 指数 日足", False),
    ("/markets/trading-by-type", {"from": "2024-06-01", "to": "2024-06-30"}, "投資部門別売買状況（需給スリーブ）", False),
    ("/markets/margin-interest", {"code": PROBE_CODE}, "信用残（需給スリーブ）", False),
    ("/markets/short-selling", {}, "業種別空売り比率", False),
]

# C-4 の核心: 分足・時間足に相当しそうなパスを推測で総当たりする。
# 1つでも 200 を返せば、それが正式パスである可能性が高い。
INTRADAY_ENDPOINT_CANDIDATES: list[tuple[str, dict]] = [
    ("/equities/bars/minute", {"code": PROBE_CODE, "date": "2024-06-03"}),
    ("/equities/bars/intraday", {"code": PROBE_CODE, "date": "2024-06-03"}),
    ("/equities/bars/1m", {"code": PROBE_CODE, "date": "2024-06-03"}),
    ("/equities/bars/hourly", {"code": PROBE_CODE, "date": "2024-06-03"}),
    ("/equities/bars/am", {"code": PROBE_CODE, "date": "2024-06-03"}),
    ("/equities/prices/am", {"code": PROBE_CODE, "date": "2024-06-03"}),
    ("/equities/prices/minute", {"code": PROBE_CODE, "date": "2024-06-03"}),
]


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


def sanity_check(api_key: str, log: list[str]) -> bool:
    """まず軽いエンドポイントでキー自体が有効かを確認する。"""
    log.append("### 0. APIキーの有効性チェック\n")
    status, body, err = http_json(f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE}&date=2024-06-03",
                                   headers=v2_headers(api_key))
    if status == 200:
        log.append(f"- `/equities/bars/daily` への疎通: **成功**（HTTP 200）")
        log.append("")
        return True
    if status in (401, 403):
        # コード桁数の違いを疑い、別表記でも試す
        status2, body2, err2 = http_json(
            f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE_ALT}&date=2024-06-03",
            headers=v2_headers(api_key),
        )
        if status2 == 200:
            log.append("- `/equities/bars/daily` への疎通: **成功**（HTTP 200、銘柄コード4桁表記）")
            log.append("")
            return True
        log.append(f"- `/equities/bars/daily` への疎通: **失敗**（HTTP {status} / {err[:120]}）")
        log.append(
            "- → APIキー自体が無効、期限切れ、またはプラン未契約の可能性が高い。"
            "J-Quants マイページでキーの状態・契約プランを確認すること。"
        )
        log.append("")
        return False
    log.append(f"- `/equities/bars/daily` への疎通: **予期しない結果**（HTTP {status} / {err[:120]}）")
    log.append("")
    return False


def probe_endpoints(api_key: str, log: list[str]) -> None:
    headers = v2_headers(api_key)
    log.append("### 1. エンドポイント別のアクセス可否（＝契約プランで何が使えるか）\n")
    log.append("| エンドポイント | 用途 | 結果 |")
    log.append("|---|---|---|")
    for path, params, purpose, confirmed in ENDPOINT_PROBES:
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
            verdict = f"✅ 利用可（{n}件）"
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


def probe_intraday(api_key: str, log: list[str]) -> None:
    """C-4 の核心。分足・時間足エンドポイントの候補を総当たりする。"""
    headers = v2_headers(api_key)
    log.append("### 2. 分足・時間足データの有無（重大論点 C-4 の決着材料）\n")
    log.append(
        "以下は**推測パスの総当たり**である。1つでも 200 が返れば、それが正式な分足エンドポイントである可能性が高い。"
        "全滅した場合、少なくとも本スクリプトが試した範囲では分足の提供を確認できなかったことを意味する"
        "（正式パスがまだ特定できていない可能性は残る）。\n"
    )
    log.append("| エンドポイント（推測） | 結果 |")
    log.append("|---|---|")
    any_hit = False
    for path, params in INTRADAY_ENDPOINT_CANDIDATES:
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        status, body, err = http_json(f"{JQUANTS_V2_BASE}{path}{qs}", headers=headers)
        if status == 200:
            verdict = "✅ **200 成功 — 分足/時間足エンドポイントの可能性大**"
            any_hit = True
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


def probe_history_range(api_key: str, log: list[str]) -> None:
    """日足がどこまで遡れるか＝選定/確認分割が成立するかを実測する。"""
    headers = v2_headers(api_key)
    log.append("### 3. 日足の遡及可能範囲（PJ000001 §6.2 の選定/確認分割が成立するか）\n")
    log.append("| 日付 | データ有無 |")
    log.append("|---|---|")
    oldest_ok = None
    for date in YEAR_PROBE_DATES:
        status, body, _ = http_json(
            f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE}&date={date}",
            headers=headers,
        )
        found = status == 200 and body and any(isinstance(v, list) and v for v in body.values())
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


def probe_delay(api_key: str, log: list[str]) -> None:
    """最新データがいつのものか＝遅延の実測。無料プランは12週間遅延とされる。"""
    headers = v2_headers(api_key)
    log.append("### 4. データ遅延の実測（無料プランは12週間遅延とされる）\n")
    today = dt.date.today()
    latest = None
    for back in range(0, 210, 7):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        status, body, _ = http_json(
            f"{JQUANTS_V2_BASE}/equities/bars/daily?code={PROBE_CODE}&date={d.isoformat()}",
            headers=headers,
        )
        if status == 200 and body and any(isinstance(v, list) and v for v in body.values()):
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
    if api_key and sanity_check(api_key, log):
        probe_endpoints(api_key, log)
        probe_intraday(api_key, log)
        probe_history_range(api_key, log)
        probe_delay(api_key, log)
    elif api_key:
        log.append("APIキーが無効と判定されたため、以降のプローブは実施しなかった。")
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
