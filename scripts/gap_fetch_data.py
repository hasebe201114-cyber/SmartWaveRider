#!/usr/bin/env python3
"""EXP-OBS000003（非決算オーバーナイト・ギャップ）データ取得スクリプト。

spec: `research/EXP-OBS000003/01-spec.md` §11。

取得対象:
  1. `/equities/master` の選定期間確定日 `D_u = T[61]`（2024-09-18）時点のスナップショット。
     §5.4 の選定期間ユニバース構築に使う（確認期間は PEAD の既存キャッシュ
     `master_confirmation_start_2025-07-01.json` を再利用するため新規取得は不要）。
  2. `/fins/earnings-date`（§7 D項。決算発表予定日。G2 パイプラインで使用）。

`/equities/bars/daily`・`/fins/summary` は EXP-OBS000001（PEAD）が取得済みの
`data/raw/pead/` を再利用する（spec §9 の前提どおり新規フェッチしない）。

使い方:
    python scripts/gap_fetch_data.py --step master
    python scripts/gap_fetch_data.py --step earnings_date

環境変数: JQUANTS_API_KEY（必須。.env.local は読まない）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.jquants_client import JQuantsClient, business_days, stderr_log  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "gap"
PEAD_RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"

# T[61]（spec §2.1 選定期間のユニバース確定日 D_u）。実測値: research/EXP-OBS000003/10-result/params.json
SELECTION_DU = dt.date(2024, 9, 18)
CONTRACT_START = dt.date(2024, 6, 21)
CONTRACT_END = dt.date(2026, 6, 21)


def load_candidate_codes() -> list[str]:
    p = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result" / "candidate_codes.json"
    return json.loads(p.read_text(encoding="utf-8"))["codes"]


def fetch_master(client: JQuantsClient, force: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"master_selection_du_{SELECTION_DU.isoformat()}.json"
    if out_path.exists() and not force:
        stderr_log(f"[master] 既存キャッシュを使用: {out_path}")
        return
    stderr_log(f"[master] 取得中: date={SELECTION_DU.isoformat()}")
    body = client.get("/equities/master", {"date": SELECTION_DU.isoformat()})
    if not JQuantsClient.has_real_data(body):
        raise RuntimeError(f"[master] date={SELECTION_DU.isoformat()} が HTTP 200 だが実データが空")
    out_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    stderr_log(f"[master] 保存: {out_path}（{len(body['data'])}件）")


def fetch_earnings_date(client: JQuantsClient, force: bool = False) -> None:
    """`/fins/earnings-date`。spec §7 D項: code/date/scheduled_date で引ける。

    候補銘柄511件それぞれについてコード指定で全期間分をまとめて取得する
    （`/equities/bars/daily` と同じパターンが有効であることを想定。429時は指数バックオフ）。
    """
    out_dir = RAW_DIR / "earnings_date"
    out_dir.mkdir(parents=True, exist_ok=True)
    codes = load_candidate_codes()
    stderr_log(f"[earnings_date] 対象銘柄数={len(codes)}")
    ok = 0
    empty = 0
    failed: list[str] = []
    for i, code in enumerate(codes, 1):
        out_path = out_dir / f"{code}.json"
        if out_path.exists() and not force:
            ok += 1
            continue
        try:
            body = client.get("/fins/earnings-date", {"code": code})
        except Exception as e:  # noqa: BLE001
            stderr_log(f"[earnings_date] code={code} 取得失敗: {e}")
            failed.append(code)
            continue
        data = body.get("data", [])
        out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if data:
            ok += 1
        else:
            empty += 1
        if i % 50 == 0:
            stderr_log(f"[earnings_date] {i}/{len(codes)} 完了")
    stderr_log(f"[earnings_date] 完了: ok={ok} empty={empty} failed={len(failed)}")
    if failed:
        (out_dir / "_failed_codes.json").write_text(
            json.dumps(failed, ensure_ascii=False), encoding="utf-8"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True, choices=["master", "earnings_date"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    client = JQuantsClient(log_fn=stderr_log)
    if args.step == "master":
        fetch_master(client, force=args.force)
    elif args.step == "earnings_date":
        fetch_earnings_date(client, force=args.force)
    stderr_log(f"[stats] request_count={client.request_count} retry_count={client.retry_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
