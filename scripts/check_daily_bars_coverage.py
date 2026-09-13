#!/usr/bin/env python3
"""EXP-OBS000001 spec §8.1 — R-1d継続確認: 日足カバレッジの月次集計（単体スクリプト）。

## 位置づけ

`research/EXP-OBS000001/00-spec.md` §8.1 が要求する「175銘柄×2024-06-21〜
2026-06-21の日足カバレッジを月次集計する」を、**メインのバックテスト実装
（`scripts/exp_obs000001_pead.py`）とは分離して単体で高速に実行できるように
したスクリプト**である。

分離する理由（前セッションの教訓）: フル実行の中で特定エンドポイントだけが
429に阻まれ続けると、他の正常な部分まで巻き込んで結果が虫食いになる
（`research/STEP0-api-probe-report.md` の経緯）。まずこの単体スクリプトで
「日足が全期間・対象銘柄で連続取得できるか」という Kill 条件（R-1d）だけを
決着させ、欠測が無いことを確認してからフル実装に進む運用とする。

## 実行方法（司令塔がローカルWindows環境で実行する手順）

1. `.env.local` に `JQUANTS_API_KEY=...` を設定した状態で、リポジトリ直下から:

       python scripts/check_daily_bars_coverage.py

2. 標準ライブラリのみで動作する（依存パッケージのインストール不要）。
3. 実行には数分〜数十分かかる見込み（レート制限対策で1リクエスト5秒間隔、
   429時は10秒待って最大3回リトライするため）。進捗はコンソールに逐次表示される。
4. 生成される成果物:
   - `research/EXP-OBS000001/10-result/daily-bars-coverage.json`
     （銘柄×月のカバレッジ表・集計・使用したクエリ戦略・候補銘柄の抽出条件）
   - `research/EXP-OBS000001/10-result/coverage-check.log`
     （実行ログ。429とデータ欠損を区別して記録。再現用コマンドを含む）
   - `data/cache/jquants_v2/` 配下に確定的な応答のキャッシュ（再実行時にAPIを
     叩き直さないため。429・通信エラーはキャッシュしない）
5. 上記2ファイルをコミットし、`research/ACTIVE.md` の状況を更新した上でpushする。
6. **`daily-bars-coverage.json` の `kill_check.months_aggregate_below_50pct` が
   空でない場合、spec §8.1 の指示通り実装を先に進めず S戦略チームへ差し戻すこと。**
   このスクリプト自身は差し戻しの要否を自動判定しない（後述「判定に関する注記」）。

## 判定に関する注記（自己解釈で決め打ちしていない点）

spec §8.1 の「欠測月（取得率が50%未満の月）が1つでも存在する場合」という文言は、
(a) 175銘柄×月のカバレッジ表の**個々のセル**（ある銘柄のある月）を指すのか、
(b) その月の**全銘柄集計**（平均・中央値）を指すのか、一意に確定できない。
新規上場銘柄は上場前の月について当然カバレッジ0%になりうるため、(a)の字義通りの
解釈だとAPIの欠陥ではなく銘柄の上場時期によって機械的にKill相当と判定されてしまう
おそれがある。本スクリプトはこの曖昧さを自己判断で解消せず、**両方の粒度を出力し、
どちらの基準で見るかの判断はS戦略チーム/司令塔に委ねる**（出力の
`monthly_coverage_by_code`＝セル単位と、`monthly_aggregate`＝月次集計の両方を
`daily-bars-coverage.json` に含める）。

## `/equities/bars/daily` のクエリ方式に関する注記

175銘柄×約500営業日を1件ずつ（`code`+`date`のペア指定のみ）取得すると
8万件超のリクエストになり、5秒間隔でも4.6日を要し非現実的である。
本スクリプトは実行時に安価な数回のプローブで「`code`のみ／`from`+`to`併用で
レンジ取得できるか」「`date`のみで全銘柄1日分が返るか」を実測してから
効率的な方式を選ぶ（`scripts/lib/jquants_client.py` の
`detect_bars_daily_strategy`）。**いずれも確認できない場合は、憶測で全件走査に
入らずここで停止する。**

## ユニバース定義との関係（重要な限定）

本スクリプトが対象にする銘柄集合は、spec §2.2 が定義する「シーズン単位で
価格・流動性条件を適用した確定175銘柄」そのものではない。**シーズン境界の
確定には決算開示イベント（`/fins/summary`）の収集が必要で、それ自体が
本スクリプト（日足カバレッジの事前疎通確認）より後段の作業**であるため、
構造的な循環になる。したがって本スクリプトでは、spec §2.2 の条件のうち
**価格・出来高に依存しない構造条件（`ScaleCat`・`Mrgn`）のみ**で候補銘柄を
抽出し、これを「日足カバレッジの事前確認に使う候補プール」として扱う
（`params.universe_note` に明記する）。**この候補プールは spec が最終的に
使う175銘柄と一致しない場合がある。** 正式な175銘柄の確定と本番のG1/G2測定は
`scripts/exp_obs000001_pead.py` 側で改めてシーズン単位に行う。

セキュリティ方針（CLAUDE.md 準拠）: 認証情報は `.env.local` からのみ読み、
値は一切出力・キャッシュしない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from statistics import mean, median

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.jquants_client import (  # noqa: E402
    BarsDailyStrategy,
    FetchOutcome,
    JQuantsClient,
    UniverseDefinitionError,
    detect_bars_daily_strategy,
    get_api_key,
    load_env_local,
)
from lib.pead_bars import (  # noqa: E402
    build_market_calendar,
    fetch_bars_range_by_code,
    fetch_bars_snapshot_by_date,
)
from lib.pead_universe import load_master_candidates  # noqa: E402
from step0_api_probe import sanity_check  # noqa: E402

RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"
COVERAGE_JSON_PATH = RESULT_DIR / "daily-bars-coverage.json"
LOG_PATH = RESULT_DIR / "coverage-check.log"

# spec §8.1 が指定する契約範囲。sanity_check() が実測する契約範囲と食い違う場合は
# 実測値を優先し、その旨を出力に明記する（spec記載値を盲信しない）。
SPEC_EXPECTED_START = dt.date(2024, 6, 21)
SPEC_EXPECTED_END = dt.date(2026, 6, 21)

COVERAGE_KILL_THRESHOLD = 0.50  # spec §8.1: 取得率50%未満の月＝欠測月


def month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def compute_coverage(
    bars_by_code: dict[str, list[dict]], calendar: list[str]
) -> tuple[dict, dict]:
    """銘柄×月のカバレッジ表（セル単位）と、月次集計を計算する。"""
    calendar_by_month: dict[str, list[str]] = {}
    for d in calendar:
        calendar_by_month.setdefault(d[:7], []).append(d)

    per_code_month: dict[str, dict[str, dict]] = {}
    for code, recs in bars_by_code.items():
        code_dates = {str(r["Date"]) for r in recs if r.get("Date")}
        per_code_month[code] = {}
        for m, days_in_month in calendar_by_month.items():
            n_expected = len(days_in_month)
            n_have = sum(1 for d in days_in_month if d in code_dates)
            rate = (n_have / n_expected) if n_expected else 0.0
            per_code_month[code][m] = {
                "expected_trading_days": n_expected,
                "bars_found": n_have,
                "coverage_rate": round(rate, 4),
            }

    monthly_aggregate: dict[str, dict] = {}
    for m in sorted(calendar_by_month.keys()):
        rates = [per_code_month[c][m]["coverage_rate"] for c in per_code_month]
        if rates:
            monthly_aggregate[m] = {
                "n_codes": len(rates),
                "mean_coverage_rate": round(mean(rates), 4),
                "median_coverage_rate": round(median(rates), 4),
                "min_coverage_rate": round(min(rates), 4),
                "n_codes_below_50pct": sum(1 for r in rates if r < COVERAGE_KILL_THRESHOLD),
            }
    return per_code_month, monthly_aggregate


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="data/cache/jquants_v2 のキャッシュを使わず、常に実際にAPIを叩く",
    )
    args = parser.parse_args()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    log.append("# EXP-OBS000001 spec §8.1 — 日足カバレッジ月次集計 実行ログ\n")
    log.append(f"- 実行日時: {dt.datetime.now().isoformat(timespec='seconds')}")
    log.append("- 再現用実行コマンド: `python scripts/check_daily_bars_coverage.py`")
    log.append("- 本ログに認証情報は一切含まれない\n")

    env = load_env_local()
    if not env:
        log.append("⚠️ `.env.local` が見つからないか空。`JQUANTS_API_KEY` を設定すること。")
        LOG_PATH.write_text("\n".join(log), encoding="utf-8")
        print("\n".join(log))
        return 1

    api_key = get_api_key(env, log)
    if not api_key:
        LOG_PATH.write_text("\n".join(log), encoding="utf-8")
        print("\n".join(log))
        return 1

    client = JQuantsClient(api_key, use_cache=not args.no_cache, log_fn=log.append)

    print("[1/5] APIキー有効性チェック・契約範囲の実測中...", flush=True)
    ok, sub_start, sub_end, valid_date, valid_code = sanity_check(api_key, log)
    if not ok or not valid_date:
        log.append("APIキーの有効性チェックに失敗したため、以降の処理を中断した。")
        LOG_PATH.write_text("\n".join(log), encoding="utf-8")
        print("\n".join(log))
        return 1

    contract_start = sub_start or SPEC_EXPECTED_START
    contract_end = sub_end or SPEC_EXPECTED_END
    log.append(
        f"- 実測された契約範囲: {contract_start.isoformat()} 〜 {contract_end.isoformat()}"
    )
    if (contract_start, contract_end) != (SPEC_EXPECTED_START, SPEC_EXPECTED_END):
        log.append(
            f"- ⚠️ spec記載の想定範囲（{SPEC_EXPECTED_START.isoformat()}〜"
            f"{SPEC_EXPECTED_END.isoformat()}）と一致しない。実測値を優先して以降を進める。"
        )
    log.append("")

    print("[2/5] `/equities/bars/daily` のクエリ方式を検出中...", flush=True)
    probe_range_from = contract_start.isoformat()
    probe_range_to = (contract_start + dt.timedelta(days=60)).isoformat()
    strategy = detect_bars_daily_strategy(
        client,
        probe_code=valid_code,
        probe_date=valid_date,
        range_from=probe_range_from,
        range_to=probe_range_to,
        log=log,
    )

    if strategy == BarsDailyStrategy.PAIR_ONLY:
        log.append(
            "**停止**: 効率的な取得方式が確認できなかったため、175銘柄規模の全期間"
            "カバレッジ取得を実行しなかった（憶測での全件走査を避けるため）。"
            "S戦略チーム/司令塔へ、J-Quants API リファレンスでの正式なクエリ方式の"
            "確認を依頼すること。"
        )
        LOG_PATH.write_text("\n".join(log), encoding="utf-8")
        print("\n".join(log))
        return 1

    print("[3/5] 候補銘柄を抽出中（ScaleCat×Mrgn 構造条件のみ）...", flush=True)
    try:
        candidates, master_diagnostics = load_master_candidates(client, log)
    except (RuntimeError, UniverseDefinitionError) as e:
        log.append(f"\n**停止**: {e}")
        LOG_PATH.write_text("\n".join(log), encoding="utf-8")
        print("\n".join(log))
        return 1

    candidate_codes = sorted({str(r.get("Code", "")) for r in candidates if r.get("Code")})
    log.append(f"候補銘柄コード数（構造条件のみ・シーズン別価格/流動性フィルタ適用前）: {len(candidate_codes)}")
    log.append(
        "※ spec §2.2 の最終175銘柄はシーズン単位の判定基準日・価格帯・流動性条件を"
        "適用して確定するため、この候補プールとは一致しない場合がある（本ファイル冒頭の注記参照）。\n"
    )

    print(f"[4/5] 日足データを取得中（戦略: {strategy.value}）...", flush=True)
    if strategy == BarsDailyStrategy.RANGE_BY_CODE:
        bars_by_code = fetch_bars_range_by_code(
            client, candidate_codes, contract_start.isoformat(), contract_end.isoformat(), log
        )
    else:
        bars_by_code = fetch_bars_snapshot_by_date(
            client, set(candidate_codes), contract_start, contract_end, log
        )

    print("[5/5] カバレッジを集計中...", flush=True)
    calendar = build_market_calendar(bars_by_code)
    per_code_month, monthly_aggregate = compute_coverage(bars_by_code, calendar)

    months_aggregate_below_50pct = [
        m for m, agg in monthly_aggregate.items() if agg["mean_coverage_rate"] < COVERAGE_KILL_THRESHOLD
    ]
    cells_below_50pct = [
        {"code": code, "month": m, **cell}
        for code, months in per_code_month.items()
        for m, cell in months.items()
        if cell["coverage_rate"] < COVERAGE_KILL_THRESHOLD
    ]

    output = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "contract_range": {
            "start": contract_start.isoformat(),
            "end": contract_end.isoformat(),
            "spec_expected_start": SPEC_EXPECTED_START.isoformat(),
            "spec_expected_end": SPEC_EXPECTED_END.isoformat(),
            "matches_spec_expectation": (contract_start, contract_end)
            == (SPEC_EXPECTED_START, SPEC_EXPECTED_END),
        },
        "query_strategy_detected": strategy.value,
        "master_diagnostics": master_diagnostics,
        "universe_note": (
            "候補銘柄は spec §2.2 のうち価格・出来高に依存しない構造条件"
            "（ScaleCat×Mrgn）のみで抽出した事前確認用プールであり、"
            "spec が最終的に使う175銘柄（シーズン単位）とは一致しない場合がある。"
        ),
        "candidate_codes": candidate_codes,
        "n_candidate_codes": len(candidate_codes),
        "market_calendar_days_observed": len(calendar),
        "market_calendar_first": calendar[0] if calendar else None,
        "market_calendar_last": calendar[-1] if calendar else None,
        "monthly_coverage_by_code": per_code_month,
        "monthly_aggregate": monthly_aggregate,
        "kill_check": {
            "threshold": COVERAGE_KILL_THRESHOLD,
            "note": (
                "spec §8.1 の『欠測月が1つでも存在する場合は差し戻す』は、"
                "セル単位(cells_below_50pct)か月次集計単位(months_aggregate_below_50pct)か"
                "一意に確定できないため両方を出力する。判定はS戦略チーム/司令塔が行う。"
            ),
            "months_aggregate_below_50pct": months_aggregate_below_50pct,
            "n_cells_below_50pct": len(cells_below_50pct),
            "cells_below_50pct_sample": cells_below_50pct[:50],
        },
        "api_call_stats": client.summary(),
    }

    COVERAGE_JSON_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log.append("\n## 結果サマリ\n")
    log.append(f"- 検出したクエリ方式: {strategy.value}")
    log.append(f"- 候補銘柄数: {len(candidate_codes)}")
    log.append(f"- 観測された市場カレンダー営業日数: {len(calendar)}")
    log.append(f"- 月次集計で平均カバレッジ50%未満の月: {months_aggregate_below_50pct or 'なし'}")
    log.append(f"- セル単位で50%未満の件数: {len(cells_below_50pct)}")
    log.append(f"- API呼び出し統計: {client.summary()}")
    try:
        coverage_json_display = COVERAGE_JSON_PATH.relative_to(REPO_ROOT)
    except ValueError:
        coverage_json_display = COVERAGE_JSON_PATH
    log.append(f"\n出力: {coverage_json_display}")

    LOG_PATH.write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    print(f"\n--- 出力を書き出しました: {COVERAGE_JSON_PATH} / {LOG_PATH} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
