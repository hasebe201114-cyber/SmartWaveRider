"""/fins/earnings-date を PubDate 単位で市場全体スキャン（EXP-OBS000004 prescreen 用）。
窓: 2026-02-02 〜 2026-06-22（3月期決算 Q4 シーズンの初回告知と、その後の変更告知を両方含む）。
出力: ed_dates.json {date: [rows...]}
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/SmartWaveRider/scripts")
from lib.jquants_client import JQuantsClient, business_days, stderr_log

SCRATCH = Path("/tmp/claude-0/-home-user-SmartWaveRider/70b3a5f3-4777-5a64-9788-dba5cef34bd2/scratchpad")
OUT = SCRATCH / "ed_dates.json"

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
