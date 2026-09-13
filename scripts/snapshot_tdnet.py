"""TDnet適時開示インデックスの日次スナップショット保存(D-4a・STEP0 0-18a)。

背景:
  - release.tdnet.info(無料公開サイト)は公表から約34日分しか閲覧できない
    (2026-09-13 の実測: 20260810=200 / 20260801=404)。
  - J-Quants の TDnetアドオン(有償・月額¥12,650)を契約しない限り、
    過去の適時開示インデックスは遡って取得できない。
  - したがって「今日から自前で記録した分だけ将来使える」という時間非対称性が
    あり、これがD-4(a)の趣旨。実験リレー(S/B/C)には乗せない基盤整備。

技術メモ(2026-09-13 opusサブエージェントによる実測で確定):
  - 正しいホストは `www.release.tdnet.info`(アペックスの release.tdnet.info
    にはAレコードが存在せず、これが当初「到達不能」と誤診断された原因)。
  - 日次一覧は `https://www.release.tdnet.info/inbs/I_list_{PAGE:03d}_{YYYYMMDD}.html`
    で、1ページ最大100件。存在しないページはHTTP 404。
  - 各行は `<tr>` 内の `<td class="{odd|even}new-{L|M|R} kj{Time|Code|Name|Title|Xbrl|Place|Histroy}">`
    で識別できる(oddnew/evennewは交互の背景色、実データ抽出には無関係)。
  - 日付はJST基準(TDnetは日本市場の適時開示のため)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://www.release.tdnet.info/inbs"
JST = dt.timezone(dt.timedelta(hours=9))
OUT_DIR = Path(__file__).resolve().parent.parent / "research" / "_snapshots" / "tdnet"
REQUEST_INTERVAL_SEC = 2.0  # 公開サイトへの礼儀として間隔を空ける(公式レート制限は不明)
MAX_PAGES_PER_DAY = 30  # 1日3000件を超えることは想定しないための安全弁


class _RowParser(HTMLParser):
    """kjTime/kjCode/kjName/kjTitle/kjXbrl/kjPlace/kjHistroy の td を持つ行を抽出する。"""

    _FIELD_BY_SUFFIX = {
        "kjTime": "time",
        "kjCode": "code",
        "kjName": "company_name",
        "kjTitle": "title",
        "kjXbrl": "xbrl",
        "kjPlace": "exchange",
        "kjHistroy": "update_history",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._cur_row: dict | None = None
        self._cur_field: str | None = None
        self._cur_text: list[str] = []
        self._cur_href: str | None = None
        self._in_td = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag == "tr":
            self._cur_row = {}
        elif tag == "td":
            cls = attrs_d.get("class", "") or ""
            field = None
            for suffix, name in self._FIELD_BY_SUFFIX.items():
                if suffix in cls:
                    field = name
                    break
            if field:
                self._in_td = True
                self._cur_field = field
                self._cur_text = []
                self._cur_href = None
        elif tag == "a" and self._in_td and self._cur_field == "title":
            href = attrs_d.get("href")
            if href:
                self._cur_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_td:
            assert self._cur_row is not None
            text = "".join(self._cur_text).strip()
            self._cur_row[self._cur_field] = text
            if self._cur_field == "title" and self._cur_href:
                self._cur_row["pdf_url"] = self._cur_href
            self._in_td = False
            self._cur_field = None
            self._cur_text = []
            self._cur_href = None
        elif tag == "tr" and self._cur_row is not None:
            if self._cur_row.get("time") and self._cur_row.get("code"):
                self.rows.append(self._cur_row)
            self._cur_row = None

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._cur_text.append(data)


def _fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "SmartWaveRider-snapshot/1.0"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except urllib.error.URLError as e:
        raise RuntimeError(f"接続失敗: {url}: {e.reason}") from e


def fetch_day(date_str: str, log=print) -> dict:
    """YYYYMMDD形式の1日分を全ページ取得してパースする。"""
    all_rows: list[dict] = []
    pages_fetched = 0
    last_status = None
    for page in range(1, MAX_PAGES_PER_DAY + 1):
        url = f"{BASE}/I_list_{page:03d}_{date_str}.html"
        if page > 1:
            time.sleep(REQUEST_INTERVAL_SEC)
        status, body = _fetch(url)
        last_status = status
        if status == 404:
            break
        if status != 200:
            log(f"  警告: {url} -> HTTP {status}(404でも200でもない)。この日の取得を打ち切る")
            break
        parser = _RowParser()
        parser.feed(body)
        all_rows.extend(parser.rows)
        pages_fetched += 1
        if len(parser.rows) < 100:
            # 1ページ100件未満は最終ページの目印(公式ページネーション仕様は非公開のため経験則)
            break
    return {
        "date": f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}",
        "pages_fetched": pages_fetched,
        "row_count": len(all_rows),
        "rows": all_rows,
        "last_page_http_status": last_status,
        "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "www.release.tdnet.info (無料公開サイト。約34日で公開終了するため日次保存が必要)",
    }


def save_snapshot(date_str: str, force: bool = False, log=print) -> str:
    """1日分を取得して保存する。戻り値: 'saved' / 'skipped_exists' / 'no_data(404)'。"""
    iso_date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
    out_path = OUT_DIR / f"{iso_date}.json"
    if out_path.exists() and not force:
        return "skipped_exists"
    result = fetch_day(date_str, log=log)
    if result["pages_fetched"] == 0:
        # 未来日・保持期限切れ(約34日超)・当日でまだ何も開示されていない、のいずれか。
        # ここでは空ファイルを書かない(後日リトライできるようにするため)。
        return "no_data(404)"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp_path.replace(out_path)
    log(f"  保存: {out_path} ({result['row_count']}件, {result['pages_fetched']}ページ)")
    return "saved"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="今日から遡って何日分を取得するか(公開サイトの保持期限は約34日)",
    )
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD形式で1日だけ指定(省略時は今日=JST)")
    ap.add_argument("--force", action="store_true", help="既存のスナップショットも再取得して上書きする")
    args = ap.parse_args()

    today_jst = dt.datetime.now(JST).date()
    if args.date:
        target_dates = [dt.date.fromisoformat(args.date)]
    elif args.backfill_days > 0:
        target_dates = [today_jst - dt.timedelta(days=i) for i in range(args.backfill_days + 1)]
    else:
        target_dates = [today_jst]

    summary = {"saved": 0, "skipped_exists": 0, "no_data(404)": 0}
    for d in target_dates:
        date_str = d.strftime("%Y%m%d")
        print(f"[{d.isoformat()}] 取得開始")
        outcome = save_snapshot(date_str, force=args.force)
        summary[outcome] += 1
        print(f"[{d.isoformat()}] -> {outcome}")
        if d != target_dates[-1]:
            time.sleep(REQUEST_INTERVAL_SEC)

    print("---")
    print(f"完了: {json.dumps(summary, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
