#!/usr/bin/env python3
"""EXP-OBS000007 P-1（0-24）: /markets/margin-interest の実機疎通確認。

目的: HTTP 200が返るか・フィールド構成・週次/日次の粒度・ローリング窓の下限境界・
      候補ユニバースのサンプルヒット率を実測するだけの使い捨てプローブ。
      エッジの有無・採否は一切判定しない（生データのみ出力）。

前提: 環境変数 JQUANTS_API_KEY が設定されていること（.env.local は読まない）。

使い方:
    python research/EXP-OBS000007/10-result/probe_margin_interest.py

出力: 標準出力に実測結果を出す（run.log にリダイレクトして保存）。
再現性: 乱数を一切使わない（決定的）。サンプル銘柄は candidate_codes_v2.json の
        先頭5件・中央5件・末尾5件を機械的に選ぶのみで、恣意的選別はしていない。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from jquants_client import JQuantsClient  # noqa: E402

CANDIDATE_CODES_FILE = REPO_ROOT / "research" / "EXP-OBS000005" / "10-result" / "candidate_codes_v2.json"
PROBE_CODE = "72030"  # トヨタ自動車


def main() -> None:
    client = JQuantsClient(log_fn=lambda m: print(m, file=sys.stderr))

    print("=== 1. 主エンドポイント疎通 (/markets/margin-interest, code=72030) ===")
    body = client.get("/markets/margin-interest", {"code": PROBE_CODE})
    data = body["data"]
    print(f"HTTP 200 / top_level_keys={list(body.keys())} / n_records={len(data)}")
    print(f"fields={sorted(data[0].keys())}")
    print(f"first_record={data[0]}")
    print(f"last_record={data[-1]}")

    print("\n=== 2. 週次/日次の粒度確認（レコード間隔の分布） ===")
    import datetime as dt
    import collections

    dates = [d["Date"] for d in data]
    deltas = [
        (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
        for a, b in zip(dates, dates[1:])
    ]
    print(f"delta_days_counter={dict(collections.Counter(deltas))}")

    print("\n=== 3. 加算関係の確認（ShrtVol = ShrtNegVol + ShrtStdVol 等） ===")
    r = data[0]
    print(f"ShrtNegVol+ShrtStdVol == ShrtVol: {r['ShrtNegVol'] + r['ShrtStdVol'] == r['ShrtVol']}")
    print(f"LongNegVol+LongStdVol == LongVol: {r['LongNegVol'] + r['LongStdVol'] == r['LongVol']}")

    print("\n=== 4. ローリング窓 下限境界の探索（明示date指定） ===")
    for probe_date in ["2016-09-09", "2016-09-15", "2016-09-16"]:
        try:
            b = client.get("/markets/margin-interest", {"code": PROBE_CODE, "date": probe_date})
            print(f"date={probe_date} -> HTTP 200 (n={len(b.get('data', []))})")
        except Exception as e:  # noqa: BLE001
            print(f"date={probe_date} -> ERROR: {e}")

    print("\n=== 5. date指定のみ（code省略）での挙動確認 ===")
    b = client.get("/markets/margin-interest", {"date": "2026-09-11"})
    print(f"date=2026-09-11 (code省略) -> HTTP 200 (n={len(b.get('data', []))})")
    print(f"sample_record={b['data'][0]}")

    print("\n=== 6. 候補ユニバースのサンプルヒット率 ===")
    codes = json.loads(CANDIDATE_CODES_FILE.read_text())["codes"]
    print(f"candidate_codes_total={len(codes)}")
    sample = codes[:5] + codes[len(codes) // 2 : len(codes) // 2 + 5] + codes[-5:]
    print(f"sample_codes={sample}")
    hits = 0
    for code in sample:
        try:
            b = client.get("/markets/margin-interest", {"code": code})
            n = len(b.get("data", []))
            first = b["data"][0]["Date"] if n else None
            last = b["data"][-1]["Date"] if n else None
            hits += 1
            print(f"  {code}: HTTP 200 n={n} range=[{first}, {last}]")
        except Exception as e:  # noqa: BLE001
            print(f"  {code}: ERROR {e}")
    print(f"hits={hits}/{len(sample)}")

    print(f"\n=== API使用状況 ===")
    print(f"total_requests={client.request_count} retries={client.retry_count}")


if __name__ == "__main__":
    main()
