"""EXP-OBS000006（ギャップ・10年版）専用ロジック。DBベースの `DbCalendar` を提供し、
既存 `gap_engine.py` の純粋関数（σ_gap計算・候補判定等）を10年データに対して再利用する。

spec: `research/EXP-OBS000006/01-spec.md`。10Y-COMMON §6.1（決済サイクル分岐）を実装する。
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import gap_common as gc
from . import gap_engine as ge
from . import pead_common as pc
from . import jq10y_common as jc

REPO_ROOT = jc.REPO_ROOT
RAW_DIR = jc.RAW_DIR
DB_PATH = jc.DB_PATH


class DbCalendar:
    """gap_common.Calendar と同じインタフェースを、SQLite DB から構築する版。

    `codes` に指定した銘柄のみ、全期間の日足をメモリに読み込む
    （σ_gapの遡り窓は66本で足りるが、銘柄が経由した全確定日で必要になるため全期間ロードする）。
    """

    def __init__(self, conn: sqlite3.Connection, T: list[str], codes: list[str]):
        self.T = T
        self.codes = codes
        self._index = {d: i + 1 for i, d in enumerate(T)}
        self.bars_by_code: dict[str, dict[str, dict]] = {}
        self.missing_bars_codes: list[str] = []
        for code in codes:
            rows = conn.execute(
                "SELECT date,o,h,l,c,ul,ll,vo,va,adjfactor,adjo,adjh,adjl,adjc,adjvo,mktcap,ext "
                "FROM bars WHERE code=? ORDER BY date", (code,)
            ).fetchall()
            if not rows:
                self.missing_bars_codes.append(code)
                continue
            by_date = {}
            for r in rows:
                by_date[r[0]] = {
                    "Date": r[0], "O": r[1], "H": r[2], "L": r[3], "C": r[4], "UL": r[5], "LL": r[6],
                    "Vo": r[7], "Va": r[8], "AdjFactor": r[9], "AdjO": r[10], "AdjH": r[11], "AdjL": r[12],
                    "AdjC": r[13], "AdjVo": r[14], "MktCap": r[15], "ExRT": r[16],
                }
            self.bars_by_code[code] = by_date

    def idx(self, date_str: str) -> int | None:
        return self._index.get(date_str)

    def at(self, i: int) -> str | None:
        if 1 <= i <= len(self.T):
            return self.T[i - 1]
        return None

    def row(self, code: str, date_str: str | None) -> dict | None:
        if date_str is None:
            return None
        return self.bars_by_code.get(code, {}).get(date_str)

    def business_day_on_or_before(self, calendar_date: dt.date) -> str | None:
        s = calendar_date.isoformat()
        pos = bisect.bisect_right(self.T, s)
        if pos == 0:
            return None
        return self.T[pos - 1]


# ---------------------------------------------------------------------------
# 10Y-COMMON §6.1: 決済サイクル分岐を組み込んだ配当落ち日構成
# ---------------------------------------------------------------------------


def build_e2a_10y(cal: DbCalendar, basis_records: list[dict]) -> dict:
    """spec §3.1.1 step 1〜5。§6.1の決済サイクル分岐を使う。"""
    by_code_x_pos: dict[tuple[str, str], list[float]] = defaultdict(list)
    explicit_zero_codes: set[str] = set()
    positive_codes: set[str] = set()
    any_record_codes: set[str] = set()
    no_mapping_count = 0
    below_t1_count = 0
    transition_band_count = 0
    cycle_counter = {"t2": 0, "t3": 0, "transition": 0}

    for r in basis_records:
        code = r["code"]
        any_record_codes.add(code)
        b = cal.business_day_on_or_before(r["basis_date"])
        if b is None:
            no_mapping_count += 1
            continue
        k = cal.idx(b)
        if k is None or k <= 2:
            below_t1_count += 1
            continue
        x_indices, cycle = jc.settlement_cycle_x_indices_for_date(b, k)
        cycle_counter[cycle] += 1
        if cycle == "transition":
            transition_band_count += 1
        for xi in x_indices:
            if xi < 1:
                below_t1_count += 1
                continue
            x = cal.at(xi)
            if x is None:
                continue
            if r["dps"] > 0:
                positive_codes.add(code)
                by_code_x_pos[(code, x)].append(r["dps"])
            elif r["is_explicit_zero"]:
                explicit_zero_codes.add(code)

    duplicate_pairs = {k: v for k, v in by_code_x_pos.items() if len(v) > 1}
    e2a_map: dict[tuple[str, str], float] = {k: max(v) for k, v in by_code_x_pos.items()}

    return {
        "e2a_map": e2a_map,
        "positive_codes": positive_codes,
        "explicit_zero_only_codes": explicit_zero_codes - positive_codes,
        "any_record_codes": any_record_codes,
        "no_mapping_count": no_mapping_count,
        "below_t1_count": below_t1_count,
        "transition_band_count": transition_band_count,
        "cycle_counter": cycle_counter,
        "duplicate_pairs_count": len(duplicate_pairs),
    }


def build_e2b_10y(cal: DbCalendar) -> dict[str, list[str]]:
    """spec §3.1.1 E-2B。各暦月の最終営業日から、§6.1の分岐で1つまたは2つのX(m)を構成する。"""
    by_month: dict[str, list[str]] = defaultdict(list)
    for d in cal.T:
        by_month[d[:7]].append(d)
    result: dict[str, list[str]] = {}
    for m, days in by_month.items():
        days.sort()
        last_day = days[-1]
        k = cal.idx(last_day)
        if k is None or k <= 2:
            continue
        x_indices, cycle = jc.settlement_cycle_x_indices_for_date(last_day, k)
        xs = [cal.at(xi) for xi in x_indices if xi >= 1 and cal.at(xi) is not None]
        result[m] = xs
    return result


def e2b_date_set(e2b_by_month: dict[str, list[str]]) -> set[str]:
    out: set[str] = set()
    for xs in e2b_by_month.values():
        out.update(xs)
    return out


# ---------------------------------------------------------------------------
# V-1〜V-6 検証（配当落ち識別が本物であることの証明）
# ---------------------------------------------------------------------------


def compute_theoretical_drop_ratio(cal: DbCalendar, code: str, x: str, dps: float) -> float | None:
    i = cal.idx(x)
    if i is None or i <= 1:
        return None
    prev_date = cal.at(i - 1)
    row_prev = cal.row(code, prev_date)
    if row_prev is None:
        return None
    c_prev = row_prev.get("C")
    if c_prev is None or float(c_prev) == 0:
        return None
    return dps / float(c_prev)


def run_v1_v6(cal: DbCalendar, e2a_result: dict, codes: list[str]) -> dict:
    """V-1（全体傾き）・V-2（プラセボ）・V-3（日別再現性）・V-4（カバレッジ）・
    V-5（期末日算術検算は9-2bで別途）・V-6（T+3期の独立検証）を評価する。"""
    e2a_map = e2a_result["e2a_map"]
    pairs = list(e2a_map.items())  # (code, x) -> dps

    def gather(shift: int, filter_fn=None) -> tuple[list[float], list[float]]:
        ds, gs = [], []
        for (code, x), dps in pairs:
            if filter_fn is not None and not filter_fn(x):
                continue
            i = cal.idx(x)
            if i is None:
                continue
            i2 = i + shift
            date2 = cal.at(i2)
            if date2 is None:
                continue
            d_ratio = compute_theoretical_drop_ratio(cal, code, x, dps)
            g = ge.gc.gap_g(cal, code, date2) if shift == 0 else _gap_g_shifted(cal, code, i2)
            if d_ratio is None or g is None:
                continue
            ds.append(d_ratio)
            gs.append(g)
        return ds, gs

    def _gap_g_shifted(cal_, code, i2):
        date_i2 = cal_.at(i2)
        if date_i2 is None or i2 <= 1:
            return None
        prev = cal_.at(i2 - 1)
        row_i = cal_.row(code, date_i2)
        row_prev = cal_.row(code, prev)
        if row_i is None or row_prev is None:
            return None
        o, c = row_i.get("O"), row_prev.get("C")
        if o is None or c is None or float(o) <= 0 or float(c) <= 0:
            return None
        return math.log(float(o) / float(c))

    ds0, gs0 = gather(0)
    fit_v1 = gc.ols(ds0, gs0)

    placebo = {}
    for shift in (-2, -1, 1, 2):
        ds_s, gs_s = gather(shift)
        placebo[str(shift)] = gc.ols(ds_s, gs_s)

    # V-3: 日別再現性（3月末・9月末の主要な落ち日ごとに個別回帰）
    by_x: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for (code, x), dps in pairs:
        d_ratio = compute_theoretical_drop_ratio(cal, code, x, dps)
        g = ge.gc.gap_g(cal, code, x)
        if d_ratio is not None and g is not None:
            by_x[x].append((d_ratio, g))
    v3_results = {}
    in_range_count = 0
    total_days = 0
    for x, pts in by_x.items():
        if len(pts) < 5:
            continue
        total_days += 1
        ds_x = [p[0] for p in pts]
        gs_x = [p[1] for p in pts]
        fit_x = gc.ols(ds_x, gs_x)
        b = fit_x.get("b")
        in_range = b is not None and -1.15 <= b <= -0.70
        if in_range:
            in_range_count += 1
        v3_results[x] = {"n": len(pts), "b": b, "in_range": in_range}
    v3_rate = (in_range_count / total_days) if total_days else None

    # V-4: カバレッジ（基準日を構成できた銘柄比率）
    v4_rate = (len(e2a_result["any_record_codes"]) and
               len(e2a_result["positive_codes"]) / max(1, len(e2a_result["any_record_codes"])))

    # V-6: T+3期のみプール
    ds_t3, gs_t3 = gather(0, filter_fn=lambda x: x <= "2019-07-12")
    fit_t3 = gc.ols(ds_t3, gs_t3)
    placebo_t3 = {}
    for shift in (-2, -1, 1, 2):
        ds_s, gs_s = gather(shift, filter_fn=lambda x: x <= "2019-07-12")
        placebo_t3[str(shift)] = gc.ols(ds_s, gs_s)

    def slope_in_range(b):
        return b is not None and -1.15 <= b <= -0.70

    def placebo_ok(p):
        return all((v.get("b") is None) or abs(v["b"]) <= 0.35 for v in p.values())

    v1_pass = slope_in_range(fit_v1.get("b"))
    v2_pass = placebo_ok(placebo)
    v3_pass = v3_rate is not None and v3_rate >= 0.8
    v4_pass = v4_rate is not None and v4_rate >= 0.99
    v6_pass = slope_in_range(fit_t3.get("b")) and placebo_ok(placebo_t3)

    return {
        "V1_overall_slope": fit_v1, "V1_pass": v1_pass,
        "V2_placebo": placebo, "V2_pass": v2_pass,
        "V3_daily_reproducibility_rate": v3_rate, "V3_days_evaluated": total_days, "V3_pass": v3_pass,
        "V4_coverage_rate": v4_rate, "V4_pass": v4_pass,
        "V6_t3_slope": fit_t3, "V6_t3_placebo": placebo_t3, "V6_pass": v6_pass,
        "all_v_pass": v1_pass and v2_pass and v3_pass and v4_pass and v6_pass,
    }
