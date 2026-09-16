#!/usr/bin/env python3
"""`data/raw/jq10y/*_by_date/*.json` を SQLite に集約する（10年分・約1000万行規模のため）。

出力: `data/raw/jq10y/jq10y.db`
テーブル:
  - bars(date, code, o,h,l,c,ul,ll,vo,va,adjfactor,adjo,adjh,adjl,adjc,adjvo,mktcap,ext)
  - fins_summary(disc_date, code, disc_time, disc_no, doctype, curpertype, json_blob)
  - earnings_date(pubdate, schdate, code, json_blob)
  - master(date, code, scalecat, mrgn, mrgnnm, s33, s33nm, json_blob)

`data/raw/jq10y/` の生JSONファイル自体が永続化された生データそのものであり
（D-18/N-13）、このDBはそこからの派生キャッシュ（削除・再構築可能）である。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "jq10y"
DB_PATH = RAW_DIR / "jq10y.db"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_bars(conn: sqlite3.Connection, force: bool = False) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS bars")
    cur.execute(
        """
        CREATE TABLE bars (
            date TEXT, code TEXT, o REAL, h REAL, l REAL, c REAL,
            ul TEXT, ll TEXT, vo REAL, va REAL, adjfactor REAL,
            adjo REAL, adjh REAL, adjl REAL, adjc REAL, adjvo REAL,
            mktcap REAL, ext TEXT
        )
        """
    )
    files = sorted((RAW_DIR / "bars_by_date").glob("*.json"))
    log(f"[bars] 対象ファイル数: {len(files)}")
    batch = []
    t0 = time.time()
    for i, fp in enumerate(files, 1):
        recs = json.loads(fp.read_text(encoding="utf-8"))
        for r in recs:
            batch.append(
                (
                    r.get("Date"), r.get("Code"), r.get("O"), r.get("H"), r.get("L"), r.get("C"),
                    r.get("UL"), r.get("LL"), r.get("Vo"), r.get("Va"), r.get("AdjFactor"),
                    r.get("AdjO"), r.get("AdjH"), r.get("AdjL"), r.get("AdjC"), r.get("AdjVo"),
                    r.get("MktCap"), r.get("ExRT"),
                )
            )
        if len(batch) >= 200_000:
            cur.executemany(
                "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch
            )
            conn.commit()
            batch = []
        if i % 200 == 0 or i == len(files):
            log(f"[bars] {i}/{len(files)} ファイル処理済み（経過 {time.time()-t0:.0f}秒）")
    if batch:
        cur.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
    log("[bars] インデックス作成中...")
    cur.execute("CREATE INDEX idx_bars_date ON bars(date)")
    cur.execute("CREATE INDEX idx_bars_code ON bars(code)")
    cur.execute("CREATE INDEX idx_bars_code_date ON bars(code, date)")
    conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    n_dates = cur.execute("SELECT COUNT(DISTINCT date) FROM bars").fetchone()[0]
    n_codes = cur.execute("SELECT COUNT(DISTINCT code) FROM bars").fetchone()[0]
    log(f"[bars] 完了: 行数={n} 日付数={n_dates} 銘柄数={n_codes}")


def build_fins_summary(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS fins_summary")
    cur.execute(
        """
        CREATE TABLE fins_summary (
            disc_date TEXT, disc_time TEXT, code TEXT, disc_no TEXT,
            doctype TEXT, curpertype TEXT, json_blob TEXT
        )
        """
    )
    files = sorted((RAW_DIR / "fins_summary_by_date").glob("*.json"))
    log(f"[fins_summary] 対象ファイル数: {len(files)}")
    batch = []
    for i, fp in enumerate(files, 1):
        recs = json.loads(fp.read_text(encoding="utf-8"))
        for r in recs:
            batch.append(
                (
                    r.get("DiscDate"), r.get("DiscTime"), r.get("Code"), r.get("DiscNo"),
                    r.get("DocType"), r.get("CurPerType"), json.dumps(r, ensure_ascii=False),
                )
            )
        if len(batch) >= 100_000:
            cur.executemany("INSERT INTO fins_summary VALUES (?,?,?,?,?,?,?)", batch)
            conn.commit()
            batch = []
        if i % 500 == 0 or i == len(files):
            log(f"[fins_summary] {i}/{len(files)} ファイル処理済み")
    if batch:
        cur.executemany("INSERT INTO fins_summary VALUES (?,?,?,?,?,?,?)", batch)
        conn.commit()
    cur.execute("CREATE INDEX idx_fins_code ON fins_summary(code)")
    cur.execute("CREATE INDEX idx_fins_discdate ON fins_summary(disc_date)")
    conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM fins_summary").fetchone()[0]
    log(f"[fins_summary] 完了: 行数={n}")


def build_earnings_date(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS earnings_date")
    cur.execute(
        "CREATE TABLE earnings_date (pubdate TEXT, schdate TEXT, code TEXT, json_blob TEXT)"
    )
    files = sorted((RAW_DIR / "earnings_date_by_date").glob("*.json"))
    log(f"[earnings_date] 対象ファイル数: {len(files)}")
    batch = []
    for i, fp in enumerate(files, 1):
        recs = json.loads(fp.read_text(encoding="utf-8"))
        for r in recs:
            batch.append((r.get("PubDate"), r.get("SchDate"), r.get("Code"), json.dumps(r, ensure_ascii=False)))
        if len(batch) >= 100_000:
            cur.executemany("INSERT INTO earnings_date VALUES (?,?,?,?)", batch)
            conn.commit()
            batch = []
        if i % 500 == 0 or i == len(files):
            log(f"[earnings_date] {i}/{len(files)} ファイル処理済み")
    if batch:
        cur.executemany("INSERT INTO earnings_date VALUES (?,?,?,?)", batch)
        conn.commit()
    cur.execute("CREATE INDEX idx_ed_code ON earnings_date(code)")
    cur.execute("CREATE INDEX idx_ed_pubdate ON earnings_date(pubdate)")
    conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM earnings_date").fetchone()[0]
    log(f"[earnings_date] 完了: 行数={n}")


def build_master(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS master")
    cur.execute(
        "CREATE TABLE master (date TEXT, code TEXT, scalecat TEXT, mrgn TEXT, mrgnnm TEXT, s33 TEXT, s33nm TEXT, json_blob TEXT)"
    )
    files = sorted((RAW_DIR / "master_by_date").glob("*.json"))
    log(f"[master] 対象ファイル数: {len(files)}")
    for fp in files:
        recs = json.loads(fp.read_text(encoding="utf-8"))
        batch = [
            (
                r.get("Date"), r.get("Code"), r.get("ScaleCat"), r.get("Mrgn"), r.get("MrgnNm"),
                r.get("S33"), r.get("S33Nm"), json.dumps(r, ensure_ascii=False),
            )
            for r in recs
        ]
        cur.executemany("INSERT INTO master VALUES (?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    cur.execute("CREATE INDEX idx_master_date_code ON master(date, code)")
    conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM master").fetchone()[0]
    log(f"[master] 完了: 行数={n}")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", nargs="+", default=["bars", "fins_summary", "earnings_date", "master"])
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")

    if "bars" in args.tables:
        build_bars(conn)
    if "fins_summary" in args.tables:
        build_fins_summary(conn)
    if "earnings_date" in args.tables:
        build_earnings_date(conn)
    if "master" in args.tables:
        build_master(conn)

    conn.close()
    log(f"saved: {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
