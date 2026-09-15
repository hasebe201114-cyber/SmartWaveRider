#!/usr/bin/env python3
"""EXP-OBS000001（PEAD）データ取得スクリプト。

spec §11 の想定に対応。取得したデータは `data/raw/pead/` にキャッシュし、
再実行時は既存キャッシュがあれば再利用する（決定性・再現性のため。ただし
`--force` で強制再取得できる）。

取得対象:
  1. `/equities/master` の2時点スナップショット（選定期間開始日 2024-06-21・
     確認期間開始日 2025-07-01）。§5.4 のユニバース定義に使う。
  2. `/fins/summary` の契約全期間（2024-06-21〜2026-06-21）の全開示。
     日付パラメータで平日ごとに1回叩き、全銘柄分をまとめて取得する
     （コード指定より効率的であることを実測済み）。
  3. `/equities/bars/daily` のユニバース候補全銘柄 × 契約全期間の日足。
     コード指定のみで全期間の履歴が1回のリクエストで返ることを実測済み。
     候補銘柄リストは9-1完了後に `pead_build_universe.py` が出力する
     `10-result/candidate_codes.json` を読み込んで使う。

使い方:
    python scripts/pead_fetch_data.py --step master
    python scripts/pead_fetch_data.py --step fins
    python scripts/pead_fetch_data.py --step bars --codes-file research/EXP-OBS000001/10-result/candidate_codes.json

環境変数: JQUANTS_API_KEY（必須。.env.local は読まない）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.jquants_client import JQuantsClient, RateLimitExhaustedError, business_days, stderr_log  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"

SELECTION_START = dt.date(2024, 6, 21)
CONFIRMATION_START = dt.date(2025, 7, 1)
# Standardプラン契約(0-23)後に実測した契約可能期間。/fins/summary に日付を振って境界を確認:
#   2016-09-14 => HTTP 400 "Your subscription covers the following dates: 2016-09-15 ~"
#   2016-09-15 => HTTP 200 実データ25件（=契約開始日）
#   2026-09-14 => HTTP 200 実データ103件（=直近営業日、確認できた最新日）
# `/equities/bars/daily` も同一境界（2016-09-15〜2026-09-14、トヨタ72030で2,441件）であることを
# 別途確認済み（オーケストレータ実測、2026-09-15）。
CONTRACT_START = dt.date(2016, 9, 15)
CONTRACT_END = dt.date(2026, 9, 14)


def fetch_master(client: JQuantsClient, force: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for label, date in (("selection_start", SELECTION_START), ("confirmation_start", CONFIRMATION_START)):
        out_path = RAW_DIR / f"master_{label}_{date.isoformat()}.json"
        if out_path.exists() and not force:
            stderr_log(f"[master] 既存キャッシュを使用: {out_path}")
            continue
        stderr_log(f"[master] 取得中: date={date.isoformat()}")
        body = client.get("/equities/master", {"date": date.isoformat()})
        if not JQuantsClient.has_real_data(body):
            raise RuntimeError(f"[master] date={date.isoformat()} が HTTP 200 だが実データが空")
        out_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        stderr_log(f"[master] 保存: {out_path}（{len(body['data'])}件）")


def fetch_fins_summary(client: JQuantsClient, force: bool = False) -> None:
    """契約全期間の /fins/summary を平日ごとに取得し、JSONL に集約する（DiscNo重複排除）。

    レジューム可能: 既に取得済みの日付は state ファイルに記録し、再実行時はスキップする。
    HTTP 429 はクライアント側で指数バックオフ済み。それでも解消しない日は「失敗日」として
    記録し、真の欠測（当日イベントなし=0件）と区別する。
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / "fins_summary_all.jsonl"
    state_path = RAW_DIR / "fins_summary_fetch_state.json"

    state: dict = {"done_dates": [], "failed_dates": [], "zero_dates": []}
    if state_path.exists() and not force:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    done = set(state.get("done_dates", []))
    failed = set(state.get("failed_dates", []))
    zero = set(state.get("zero_dates", []))

    days = list(business_days(CONTRACT_START, CONTRACT_END))
    stderr_log(f"[fins] 対象平日数: {len(days)}（{CONTRACT_START} 〜 {CONTRACT_END}）")

    # 既存レコードを DiscNo で dedup しつつ追記モードで開く
    seen_discno: set[str] = set()
    if out_path.exists():
        with out_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    seen_discno.add(rec.get("DiscNo", ""))
                except json.JSONDecodeError:
                    continue

    n_fetched_days = 0
    n_new_records = 0
    with out_path.open("a", encoding="utf-8") as f:
        for i, day in enumerate(days, 1):
            iso = day.isoformat()
            if iso in done or iso in zero:
                continue
            if iso in failed and not force:
                stderr_log(f"[fins] {iso}: 前回失敗のため再試行")
            stderr_log(f"[fins] ({i}/{len(days)}) 取得中: {iso}")
            try:
                body = client.get("/fins/summary", {"date": iso})
            except RateLimitExhaustedError as e:
                stderr_log(f"[fins] {iso}: 429指数バックオフ上限到達。失敗日として記録: {e}")
                failed.add(iso)
                _save_state(state_path, done, failed, zero)
                continue
            except Exception as e:  # noqa: BLE001
                stderr_log(f"[fins] {iso}: 取得エラー。失敗日として記録: {e}")
                failed.add(iso)
                _save_state(state_path, done, failed, zero)
                continue

            records = body.get("data", []) if isinstance(body, dict) else []
            if not records:
                zero.add(iso)
                failed.discard(iso)
                _save_state(state_path, done, failed, zero)
                continue

            new_count = 0
            for rec in records:
                discno = rec.get("DiscNo", "")
                if discno and discno in seen_discno:
                    continue
                if discno:
                    seen_discno.add(discno)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                new_count += 1
            n_new_records += new_count
            done.add(iso)
            failed.discard(iso)
            n_fetched_days += 1
            _save_state(state_path, done, failed, zero)

    stderr_log(
        f"[fins] 完了。新規取得日数={n_fetched_days} 新規レコード={n_new_records} "
        f"累計成功日={len(done)} 累計0件日={len(zero)} 失敗日={len(failed)}"
    )
    if failed:
        stderr_log(f"[fins] 警告: 失敗日が残っている（429起因の可能性）: {sorted(failed)}")


def _save_state(path: Path, done: set, failed: set, zero: set) -> None:
    path.write_text(
        json.dumps(
            {
                "done_dates": sorted(done),
                "failed_dates": sorted(failed),
                "zero_dates": sorted(zero),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def fetch_bars_for_codes(client: JQuantsClient, codes: list[str], force: bool = False) -> None:
    """ユニバース候補全銘柄の日足を、銘柄ごとに1リクエストで全期間分取得する。"""
    bars_dir = RAW_DIR / "bars_daily"
    bars_dir.mkdir(parents=True, exist_ok=True)
    state_path = RAW_DIR / "bars_fetch_state.json"
    state = {"done_codes": [], "failed_codes": []}
    if state_path.exists() and not force:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    done = set(state.get("done_codes", []))
    failed = set(state.get("failed_codes", []))

    stderr_log(f"[bars] 対象銘柄数: {len(codes)}")
    for i, code in enumerate(codes, 1):
        out_path = bars_dir / f"{code}.json"
        if code in done and out_path.exists() and not force:
            continue
        stderr_log(f"[bars] ({i}/{len(codes)}) 取得中: code={code}")
        try:
            body = client.get("/equities/bars/daily", {"code": code})
        except RateLimitExhaustedError as e:
            stderr_log(f"[bars] code={code}: 429指数バックオフ上限到達。失敗として記録: {e}")
            failed.add(code)
            _save_bars_state(state_path, done, failed)
            continue
        except Exception as e:  # noqa: BLE001
            stderr_log(f"[bars] code={code}: 取得エラー。失敗として記録: {e}")
            failed.add(code)
            _save_bars_state(state_path, done, failed)
            continue
        records = body.get("data", []) if isinstance(body, dict) else []
        out_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        done.add(code)
        failed.discard(code)
        _save_bars_state(state_path, done, failed)

    stderr_log(f"[bars] 完了。成功={len(done)} 失敗={len(failed)}")
    if failed:
        stderr_log(f"[bars] 警告: 失敗銘柄が残っている（429起因の可能性）: {sorted(failed)}")


def _save_bars_state(path: Path, done: set, failed: set) -> None:
    path.write_text(
        json.dumps({"done_codes": sorted(done), "failed_codes": sorted(failed)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", required=True, choices=["master", "fins", "bars"])
    parser.add_argument("--codes-file", type=str, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    client = JQuantsClient(log_fn=stderr_log)

    if args.step == "master":
        fetch_master(client, force=args.force)
    elif args.step == "fins":
        fetch_fins_summary(client, force=args.force)
    elif args.step == "bars":
        if not args.codes_file:
            print("エラー: --step bars には --codes-file が必要", file=sys.stderr)
            return 1
        codes = json.loads(Path(args.codes_file).read_text(encoding="utf-8"))
        if isinstance(codes, dict):
            codes = codes.get("codes", [])
        fetch_bars_for_codes(client, codes, force=args.force)

    stderr_log(f"[統計] APIリクエスト総数={client.request_count} 429リトライ回数={client.retry_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
