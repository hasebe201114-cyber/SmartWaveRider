"""J-Quants `/fins/earnings-date` の全市場スキャン（S戦略チームの目利き用の一次実測）。

目的:
  「決算発表予定日の前倒し/後ろ倒し」候補（EXP-OBS000003 §6.5）について、
  予定日の"変更"がそもそも何件発生するのかを実測し、独立EXPとして起票に足る
  事象数があるかを判定する。結論は却下（`research/portfolio-ledger.md` の不採用行）。

技術メモ（2026-09-13 実測で確定）:
  - 決算発表予定日の正しいエンドポイントは `/fins/earnings-date`。
    `/equities/earnings-calendar` は仕様上「3・9月期決算会社のみ」「翌営業日分のみ」を
    返す設計であり、`code`/`from`/`to` 等のパラメータを持たない（別物）。
  - パラメータは `code` / `date`（公表日）/ `scheduled_date`（発表予定日）のいずれか1つが必須。
  - レスポンス項目: PubDate / SchDate / FQName / FYE / Code / CoName / CoNameEn。
  - Free プランの契約範囲は 2024-06-22 〜 2026-06-22（範囲外は HTTP 400 で明示される）。
  - 予定日の変更履歴は公表日単位で保持される（同一 (Code, FYE, FQName) に複数レコード）。
  - レート制限が厳しく、間隔13秒でも 429 が頻発する（クライアント側の指数バックオフで吸収）。

窓: 2026-02-02 〜 2026-06-22（3月期決算 Q4 シーズンの初回告知と、その後の変更告知を両方含む）。
出力: research/_snapshots/earnings_date/scan_2026-02-02_2026-06-22.json
解析: scripts/probe_earnings_date_analyze.py
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.jquants_client import JQuantsClient, business_days, stderr_log

OUT_DIR = Path(__file__).resolve().parent.parent / "research" / "_snapshots" / "earnings_date"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = OUT_DIR / "scan_2026-02-02_2026-06-22.json"

days = [d.isoformat() for d in business_days(dt.date(2026, 2, 2), dt.date(2026, 6, 22))]
stderr_log(f"days={len(days)}")

c = JQuantsClient(min_interval=13.0, log_fn=stderr_log)
out = {}
errs = {}
for i, d in enumerate(days, 1):
    try:
        body = c.get("/fins/earnings-date", {"date": d})
        out[d] = body.get("data", [])
    except Exception as e:
        errs[d] = str(e)
        stderr_log(f"ERR {d}: {e}")
    if i % 5 == 0:
        stderr_log(f"... {i}/{len(days)} rows_so_far={sum(len(v) for v in out.values())}")
        OUT.write_text(json.dumps({"rows": out, "errors": errs}, ensure_ascii=False))

OUT.write_text(json.dumps({"rows": out, "errors": errs}, ensure_ascii=False))
stderr_log(f"DONE days={len(out)} errors={len(errs)} requests={c.request_count} retries={c.retry_count}")
