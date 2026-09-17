#!/usr/bin/env python3
"""EXP-OBS000011 (候補7: 自己株式取得発表) - prescreenレベルの件数実測。

D-10運用ルール（相異なるイベント日数・最大単日シェア・選定/確認期間の発生率比を
リターンを見ずに件数のみで先に測る）を、既存の TDnet 日次スナップショット
（research/_snapshots/tdnet/*.json）に対して適用する。

前方リターンは一切参照しない。判定・解釈は書かない（生データのみ）。
S戦略チームが直接実行した（B実装チームへの依頼ではなく、既存スナップショットの
再集計のみで完了するため）。

イベント定義（回す前に固定）:
  タイトルが正規表現 r'自己株式.{0,3}取得に係る事項の決定' に一致する開示。
  EXP-OBS000003 §6.6 が全市場40日間で81件と確認した「自己株式取得に係る事項の
  決定」開示と同一種別（表記ゆれ「自己株式取得」/「自己株式の取得」、および
  「…及び自己株式の消却…」等の複合表題を含む）。

ユニバース（回す前に固定）:
  research/_snapshots/llm_features/frozen/universe_v1.json（271銘柄・
  U-LLM-v1・EXP-OBS000001 spec §5.4 U-1〜U-5通過銘柄・2025-07-01時点）を
  そのまま再利用する。本タスク専用の新規ユニバースは定義しない
  （既存の凍結済みユニバースを流用することで、ユニバース選択そのものが
  結果を見てから行われた選別ではないことを保証する）。

出力: research/_snapshots/prescreen_candidate7/selftender_count.json
"""
import json
import glob
import re
import collections
import hashlib
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TDNET_GLOB = os.path.join(REPO_ROOT, "research/_snapshots/tdnet/*.json")
UNIVERSE_FILE = os.path.join(
    REPO_ROOT, "research/_snapshots/llm_features/frozen/universe_v1.json"
)
OUT_FILE = os.path.join(
    REPO_ROOT, "research/_snapshots/prescreen_candidate7/selftender_count.json"
)

EVENT_TITLE_PATTERN = re.compile(r"自己株式.{0,3}取得に係る事項の決定")


def annualize(n_events, n_days, business_days_per_year=245):
    if n_days == 0:
        return None
    return n_events * business_days_per_year / n_days


def main():
    files = sorted(glob.glob(TDNET_GLOB))
    universe = json.load(open(UNIVERSE_FILE))
    uni_codes = set(universe["codes"])
    uni_sha = universe["codes_sha256"]

    all_market_by_day = collections.Counter()
    uni_by_day = collections.Counter()
    uni_codes_hit = set()
    matched_rows = []

    dates_seen = []
    for f in files:
        d = json.load(open(f))
        date = d["date"]
        dates_seen.append(date)
        for r in d.get("rows", []):
            title = r.get("title", "")
            if not EVENT_TITLE_PATTERN.search(title):
                continue
            all_market_by_day[date] += 1
            code = r.get("code", "")
            if code in uni_codes:
                uni_by_day[date] += 1
                uni_codes_hit.add(code)
                matched_rows.append(
                    {
                        "date": date,
                        "code": code,
                        "company_name": r.get("company_name"),
                        "title": title,
                    }
                )

    n_files = len(files)
    date_min = dates_seen[0] if dates_seen else None
    date_max = dates_seen[-1] if dates_seen else None

    all_total = sum(all_market_by_day.values())
    all_days = len(all_market_by_day)
    all_max_day, all_max_c = (
        max(all_market_by_day.items(), key=lambda kv: kv[1])
        if all_market_by_day
        else (None, 0)
    )

    uni_total = sum(uni_by_day.values())
    uni_days = len(uni_by_day)
    uni_max_day, uni_max_c = (
        max(uni_by_day.items(), key=lambda kv: kv[1]) if uni_by_day else (None, 0)
    )

    result = {
        "event_title_pattern": EVENT_TITLE_PATTERN.pattern,
        "universe_file": os.path.relpath(UNIVERSE_FILE, REPO_ROOT),
        "universe_codes_sha256": uni_sha,
        "universe_count": len(uni_codes),
        "snapshot_window": {
            "n_files": n_files,
            "date_min": date_min,
            "date_max": date_max,
            "note": "TDnet日次スナップショットの蓄積開始からの全期間（過去への遡及取得は不可）",
        },
        "all_market": {
            "total_events": all_total,
            "distinct_event_days": all_days,
            "max_single_day": {"date": all_max_day, "count": all_max_c},
            "max_single_day_share": (all_max_c / all_total) if all_total else None,
            "annualized_events_per_year": annualize(all_total, n_files),
        },
        "universe_271_only": {
            "total_events": uni_total,
            "distinct_event_days": uni_days,
            "distinct_universe_codes_hit": len(uni_codes_hit),
            "max_single_day": {"date": uni_max_day, "count": uni_max_c},
            "max_single_day_share": (uni_max_c / uni_total) if uni_total else None,
            "annualized_events_per_year": annualize(uni_total, n_files),
        },
        "matched_rows_universe_271": matched_rows,
    }

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    print(json.dumps(result, ensure_ascii=False, indent=1))
    print("\nwrote:", os.path.relpath(OUT_FILE, REPO_ROOT))


if __name__ == "__main__":
    main()
