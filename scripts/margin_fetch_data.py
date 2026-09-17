#!/usr/bin/env python3
"""EXP-OBS000007（信用取引残高・データ保全のみ）取得スクリプト。

位置づけ: `research/ACTIVE.md` 0-25 の実行。**データ保全のみが目的**であり、
エッジ検定・spec起票・G1測定は一切行わない（判定語・解釈も書かない）。

背景: S戦略チームが候補1（信用取引残高）を条件付きGO と判定
（`research/EXP-OBS000007/00-prescreen.md` §2.1）。実機確認（0-24・
`research/EXP-OBS000007/10-result/margin_interest_probe.md`）で
`/markets/margin-interest` がHTTP 200・週次データ・候補ユニバース554銘柄で
ヒットし、取得可能期間 2016-09-16〜2026-09-11（実行日2026-09-16時点の
ローリング窓）であることを確認済み。J-Quants Standardプラン契約は
「1ヶ月以内に完了・月末までに解約」の時限運用のため（`research/ACTIVE.md` 0-23と
同じ事情）、ローリング窓が動く前に今すぐ一括取得して `data/raw/` へ永続化する。

取得対象:
  候補ユニバース554銘柄（`research/EXP-OBS000005/10-result/candidate_codes_v2.json`）
  それぞれについて `/markets/margin-interest` を `code` 指定で1回叩き、
  全期間データを取得する（銘柄ごとに1リクエストで全期間が返ることは
  0-24プローブで確認済み）。

出力先: `data/raw/margin_interest/{code}.json`（レコードのリストをそのまま保存）。
レジューム: `data/raw/margin_interest/margin_fetch_state.json` に成功/失敗銘柄を記録し、
再実行時は成功済み銘柄をスキップする（`--force` で強制再取得可能）。

**既存キャッシュの上書きに関する注意（`research/EXP-OBS000001/11-data-refetch-standard-plan.md`
「訂正: 実体はローリング窓である」節の教訓）**: 本エンドポイントもローリング窓であるため、
一度取得したキャッシュを `--force` で上書き・再取得すると、当時取得できた最古日（ローリング窓の
下限）が失われる可能性がある。通常運用では `--force` を使わないこと。

使い方:
    python scripts/margin_fetch_data.py --codes-file research/EXP-OBS000005/10-result/candidate_codes_v2.json

環境変数: JQUANTS_API_KEY（必須。.env.local は読まない）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.jquants_client import JQuantsClient, RateLimitExhaustedError, stderr_log  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "margin_interest"
DEFAULT_CODES_FILE = REPO_ROOT / "research" / "EXP-OBS000005" / "10-result" / "candidate_codes_v2.json"


def load_codes(codes_file: Path) -> list[str]:
    obj = json.loads(codes_file.read_text(encoding="utf-8"))
    codes = obj.get("codes", []) if isinstance(obj, dict) else obj
    return list(codes)


def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"done_codes": [], "failed_codes": [], "empty_codes": []}


def _save_state(state_path: Path, done: set, failed: set, empty: set) -> None:
    state_path.write_text(
        json.dumps(
            {
                "done_codes": sorted(done),
                "failed_codes": sorted(failed),
                "empty_codes": sorted(empty),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def fetch_margin_interest_for_codes(client: JQuantsClient, codes: list[str], force: bool = False) -> None:
    """候補ユニバース全銘柄の信用取引残高を、銘柄ごとに1リクエストで全期間分取得する。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    state_path = RAW_DIR / "margin_fetch_state.json"
    state = _load_state(state_path) if not force else {"done_codes": [], "failed_codes": [], "empty_codes": []}
    done = set(state.get("done_codes", []))
    failed = set(state.get("failed_codes", []))
    empty = set(state.get("empty_codes", []))

    stderr_log(f"[margin] 対象銘柄数: {len(codes)}")
    for i, code in enumerate(codes, 1):
        out_path = RAW_DIR / f"{code}.json"
        if code in done and out_path.exists() and not force:
            continue
        stderr_log(f"[margin] ({i}/{len(codes)}) 取得中: code={code}")
        try:
            body = client.get("/markets/margin-interest", {"code": code})
        except RateLimitExhaustedError as e:
            stderr_log(f"[margin] code={code}: 429指数バックオフ上限到達。失敗として記録: {e}")
            failed.add(code)
            _save_state(state_path, done, failed, empty)
            continue
        except Exception as e:  # noqa: BLE001
            stderr_log(f"[margin] code={code}: 取得エラー。失敗として記録: {e}")
            failed.add(code)
            _save_state(state_path, done, failed, empty)
            continue
        records = body.get("data", []) if isinstance(body, dict) else []
        out_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        done.add(code)
        failed.discard(code)
        if records:
            empty.discard(code)
        else:
            empty.add(code)
        _save_state(state_path, done, failed, empty)
        if i % 50 == 0:
            stderr_log(f"[margin] {i}/{len(codes)} 完了（累計成功={len(done)} 失敗={len(failed)} 0件={len(empty)}）")

    stderr_log(f"[margin] 完了。成功={len(done)} 失敗={len(failed)} 0件={len(empty)}")
    if failed:
        (RAW_DIR / "_failed_codes.json").write_text(
            json.dumps(sorted(failed), ensure_ascii=False), encoding="utf-8"
        )
        stderr_log(f"[margin] 警告: 失敗銘柄が残っている: {sorted(failed)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes-file", type=str, default=str(DEFAULT_CODES_FILE))
    ap.add_argument("--force", action="store_true", help="既存キャッシュを無視して再取得する（ローリング窓のため通常は非推奨）")
    args = ap.parse_args()

    codes = load_codes(Path(args.codes_file))
    client = JQuantsClient(log_fn=stderr_log)
    fetch_margin_interest_for_codes(client, codes, force=args.force)
    stderr_log(f"[stats] request_count={client.request_count} retry_count={client.retry_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
