#!/bin/bash
# bars取得完了後に実行する後続パイプライン（D-2/D-3/D-9 -> D-4~D-8 -> DSゲート）。
set -e
cd "$(dirname "$0")/.."

echo "=== jq10y_compute_calendar ==="
python3 scripts/jq10y_compute_calendar.py

echo "=== extract U_dates ==="
python3 -c "
import json
d = json.load(open('data/raw/jq10y/calendar.json'))
json.dump(d['U_dates'], open('/tmp/u_dates.json', 'w'))
print('U_dates:', d['U_dates'])
"

echo "=== jq10y_fetch_data --step master ==="
python3 scripts/jq10y_fetch_data.py --step master --dates-file /tmp/u_dates.json

echo "=== jq10y_build_db (bars, master) ==="
python3 scripts/jq10y_build_db.py --tables bars master

echo "=== jq10y_build_universe ==="
python3 scripts/jq10y_build_universe.py
