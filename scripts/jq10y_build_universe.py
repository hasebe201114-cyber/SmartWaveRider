#!/usr/bin/env python3
"""10Y-COMMON §5（U-1〜U-9・年次再確定・point-in-timeマスタ・フォールバック）を実装する。

前提: `jq10y_compute_calendar.py`・`jq10y_fetch_data.py --step master --dates-file <U_datesを含むJSON>`・
`jq10y_build_db.py --tables bars master` が完了していること。

出力:
  - `research/EXP-OBS000007/10-result/universe.json`
      確定日ごとの U-1〜U-5 通過集合（打ち切り前）・Va順位、および U6_cap=175/271それぞれの
      打ち切り結果を別キーで出力する（10Y-COMMON §5.2 末尾の要求）。
  - `research/EXP-OBS000007/10-result/params.json` に D-3（point-in-timeマスタ検証）の結果をマージする
    （このスクリプト単体では params_universe_fragment.json として出力し、feasibility側でマージする）。

判定語は書かない。数値と事実のみ。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "jq10y"
DB_PATH = RAW_DIR / "jq10y.db"
RESULT_DIR_SHARED = REPO_ROOT / "research" / "EXP-OBS000007" / "10-result"

SCALECAT_U1_OK = {"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"}
MRGN_U2_OK = {"1", "2"}
PRICE_BAND_MAIN = (1000.0, 3500.0)
PRICE_BAND_FALLBACK = (700.0, 8000.0)
LIQUIDITY_MIN_VA = 5e8
U7_MIN = 150
U6_CAPS = {"pead": 271, "gap": 175}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def trailing_median_va(conn: sqlite3.Connection, code: str, as_of: str, window: int = 60) -> tuple[float | None, int]:
    """確定日を含まない直前window営業日のVa中央値。有効値が30個未満なら不通過（Noneを返す）。"""
    rows = conn.execute(
        "SELECT va FROM bars WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
        (code, as_of, window),
    ).fetchall()
    vas = [r[0] for r in rows if r[0] is not None]
    if len(vas) < 30:
        return None, len(vas)
    return median(vas), len(vas)


def price_at(conn: sqlite3.Connection, code: str, as_of: str) -> float | None:
    row = conn.execute("SELECT c FROM bars WHERE code=? AND date=?", (code, as_of)).fetchone()
    return row[0] if row else None


def mktcap_fallback_top500(conn: sqlite3.Connection, as_of: str) -> tuple[set[str], dict]:
    """10Y-COMMON §5.3(ii): 確定日直前60営業日のMktCap中央値降順で上位500銘柄。"""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM bars WHERE date<? ORDER BY date DESC LIMIT 60", (as_of,)
    ).fetchall()]
    if not dates:
        return set(), {"window_dates_count": 0}
    min_d = min(dates)
    rows = conn.execute(
        "SELECT code, mktcap FROM bars WHERE date>=? AND date<? AND mktcap IS NOT NULL", (min_d, as_of)
    ).fetchall()
    by_code: dict[str, list[float]] = {}
    for code, mc in rows:
        by_code.setdefault(code, []).append(mc)
    med_by_code = {c: median(v) for c, v in by_code.items()}
    ranked = sorted(med_by_code.items(), key=lambda kv: (-kv[1], kv[0]))
    top500 = {c for c, _ in ranked[:500]}
    missing_rate = 1.0 - (len(by_code) / max(1, len(conn.execute(
        "SELECT DISTINCT code FROM bars WHERE date>=? AND date<?", (min_d, as_of)
    ).fetchall())))
    return top500, {"window_dates_count": len(dates), "window_start": min_d, "total_candidate_codes": len(by_code), "mktcap_missing_rate_in_window": missing_rate}


def load_master_snapshot(date: str) -> dict[str, dict] | None:
    fp = RAW_DIR / "master_by_date" / f"{date}.json"
    if not fp.exists():
        return None
    recs = json.loads(fp.read_text(encoding="utf-8"))
    if not recs:
        return None
    return {r["Code"]: r for r in recs}


def build_for_date(conn: sqlite3.Connection, date: str, all_codes: list[str], pit_log: list[dict]) -> dict:
    master = load_master_snapshot(date)
    branch = "i_primary"
    fallback_stats = None
    scalecat_ok_codes: set[str] | None = None

    if master is None or not any("ScaleCat" in v and v.get("ScaleCat") for v in master.values()):
        branch = "ii_fallback_mktcap_top500"
        top500, fallback_stats = mktcap_fallback_top500(conn, date)
        scalecat_ok_codes = top500
    else:
        scalecat_ok_codes = {c for c, r in master.items() if r.get("ScaleCat") in SCALECAT_U1_OK}

    pit_log.append({
        "date": date, "branch": branch, "fallback_stats": fallback_stats,
        "master_available": master is not None,
        "master_scalecat_unique_values": sorted({v.get("ScaleCat") for v in master.values()}) if master else None,
        "master_mrgn_unique_values": sorted({v.get("Mrgn") for v in master.values()}) if master else None,
    })

    rows = []
    for code in all_codes:
        if code not in scalecat_ok_codes:
            continue  # U-1
        if branch == "i_primary":
            m = master.get(code)
            if m is None:
                continue
            mrgn = m.get("Mrgn")
            if mrgn not in MRGN_U2_OK:
                continue  # U-2
            s33, s33nm = m.get("S33"), m.get("S33Nm")
        else:
            # フォールバック分岐: masterがそもそも使えないため、U-2(信用区分)はmasterに依存する。
            # masterが部分的に存在する場合はそれを使い、無ければU-2判定不能として除外する。
            m = master.get(code) if master else None
            if m is None or m.get("Mrgn") not in MRGN_U2_OK:
                continue
            s33, s33nm = m.get("S33"), m.get("S33Nm")

        va_median, va_n = trailing_median_va(conn, code, date, window=60)
        if va_median is None or va_median < LIQUIDITY_MIN_VA:
            continue  # U-4
        price = price_at(conn, code, date)
        rows.append({
            "code": code, "s33": s33, "s33_nm": s33nm,
            "va_median_60bd": va_median, "va_valid_n": va_n, "price": price,
        })

    def apply_price_band(band: tuple[float, float]) -> list[dict]:
        return [r for r in rows if r["price"] is not None and band[0] <= r["price"] <= band[1]]

    passed_main = apply_price_band(PRICE_BAND_MAIN)
    fallback_price_applied = False
    passed = passed_main
    if len(passed_main) < U7_MIN:
        fallback_price_applied = True
        passed = apply_price_band(PRICE_BAND_FALLBACK)

    # Va降順（同値はCode昇順）でランク付け（打ち切り前）
    ranked = sorted(passed, key=lambda r: (-r["va_median_60bd"], r["code"]))
    for i, r in enumerate(ranked, 1):
        r["va_rank"] = i

    caps_result = {}
    for label, cap in U6_CAPS.items():
        truncated = len(ranked) > cap
        top = ranked[:cap] if truncated else ranked
        codes_176_to_cap = [r["code"] for r in ranked[175:cap]] if cap > 175 else []
        vas = [r["va_median_60bd"] for r in top]
        caps_result[label] = {
            "u6_cap": cap,
            "u6_cap_binding": truncated,
            "final_count": len(top),
            "codes": [r["code"] for r in top],
            "va_median_distribution": {
                "min": min(vas) if vas else None,
                "p25": (sorted(vas)[len(vas)//4] if vas else None),
                "median": median(vas),
                "p75": (sorted(vas)[3*len(vas)//4] if vas else None) if vas else None,
                "max": max(vas) if vas else None,
            },
            "rank_176_to_cap_codes": codes_176_to_cap,
        }

    return {
        "date": date,
        "point_in_time_branch": branch,
        "u1_u2_pass_count": len(rows),
        "price_band_main_pass_count": len(passed_main),
        "price_band_fallback_applied": fallback_price_applied,
        "final_count_before_u6_cap": len(ranked),
        "unresolved_below_150": len(ranked) < U7_MIN,
        "by_u6_cap": caps_result,
        "va_rank_full_list_top300": [{"rank": r["va_rank"], "code": r["code"], "va_median": r["va_median_60bd"]} for r in ranked[:300]],
    }


def main() -> int:
    calendar = json.loads((RAW_DIR / "calendar.json").read_text(encoding="utf-8"))
    U_dates = calendar["U_dates"]

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA query_only=ON")

    all_codes = sorted({r[0] for r in conn.execute("SELECT DISTINCT code FROM bars").fetchall()})
    log(f"U-8 候補銘柄集合（全期間の和集合）: {len(all_codes)}銘柄")

    pit_log: list[dict] = []
    per_date_results = []
    for d in U_dates:
        log(f"確定日処理中: {d}")
        res = build_for_date(conn, d, all_codes, pit_log)
        per_date_results.append(res)
        for label in U6_CAPS:
            log(f"  [{label}] final={res['by_u6_cap'][label]['final_count']} binding={res['by_u6_cap'][label]['u6_cap_binding']} unresolved={res['unresolved_below_150']}")

    out = {
        "generated_from": "jq10y_build_universe.py",
        "rule_reference": "10Y-COMMON §5（U-1〜U-9）",
        "U_dates": U_dates,
        "candidate_codes_u8_count": len(all_codes),
        "per_date": per_date_results,
        "unresolved_dates": [r["date"] for r in per_date_results if r["unresolved_below_150"]],
    }
    RESULT_DIR_SHARED.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR_SHARED / "universe.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"saved: {out_path}")

    pit_path = RESULT_DIR_SHARED / "point_in_time_master_validation.json"
    pit_path.write_text(json.dumps(pit_log, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"saved: {pit_path}")

    if out["unresolved_dates"]:
        log(f"STOP: U-7緩和後も150銘柄未満の確定日がある: {out['unresolved_dates']}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
