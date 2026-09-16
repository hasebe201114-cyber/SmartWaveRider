"""EXP-OBS000005 / EXP-OBS000006（10年版）共通ロジック。10Y-COMMON 正本に対応。

SQLite DB (`data/raw/jq10y/jq10y.db`) と `calendar.json` / `universe.json` を読み込み、
T・確定日ごとのユニバース・SUE計算・ギャップ計算などの純粋関数を提供する。
HTTP通信は含まない。
"""

from __future__ import annotations

import bisect
import calendar as calmod
import datetime as dt
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "jq10y"
DB_PATH = RAW_DIR / "jq10y.db"
SHARED_RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000005" / "10-result"

PERM_SEED = 20260915


def get_conn(readonly: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def load_calendar() -> dict:
    return json.loads((RAW_DIR / "calendar.json").read_text(encoding="utf-8"))


def load_universe() -> dict:
    return json.loads((SHARED_RESULT_DIR / "universe.json").read_text(encoding="utf-8"))


class Calendar:
    """T（参照営業日カレンダー）。1始まり添字。"""

    def __init__(self, cal_json: dict):
        self.T: list[str] = cal_json["T"]
        self._index = {d: i + 1 for i, d in enumerate(self.T)}
        self.T1 = cal_json["T1"]
        self.T_len = cal_json["T_len"]
        self.T61 = cal_json["T61"]
        self.T68 = cal_json["T68"]
        self.split_date = cal_json["split_date"]
        self.s_idx = cal_json["s_idx"]
        self.selection_range = tuple(cal_json["selection_range"])
        self.confirmation_range = tuple(cal_json["confirmation_range"])
        self.U_dates = cal_json["U_dates"]

    def idx(self, date_str: str) -> int | None:
        return self._index.get(date_str)

    def at(self, i: int) -> str | None:
        if 1 <= i <= len(self.T):
            return self.T[i - 1]
        return None

    def business_day_on_or_before(self, calendar_date: dt.date) -> str | None:
        s = calendar_date.isoformat()
        pos = bisect.bisect_right(self.T, s)
        if pos == 0:
            return None
        return self.T[pos - 1]


class UniverseIndex:
    """確定日ごとのユニバース（10Y-COMMON §5.1 適用規則: max{d in U_dates : d<=D}）。"""

    def __init__(self, universe_json: dict, u6_cap_label: str):
        self.label = u6_cap_label
        self.per_date = {r["date"]: r for r in universe_json["per_date"]}
        self.U_dates_sorted = sorted(self.per_date.keys())

    def governing_date(self, event_date: str) -> str | None:
        pos = bisect.bisect_right(self.U_dates_sorted, event_date)
        if pos == 0:
            return None
        return self.U_dates_sorted[pos - 1]

    def codes_for(self, event_date: str) -> set[str]:
        gd = self.governing_date(event_date)
        if gd is None:
            return set()
        return set(self.per_date[gd]["by_u6_cap"][self.label]["codes"])

    def top175_codes_for(self, event_date: str) -> set[str]:
        """G1-7用: Va順位1〜175位帯（U6_capに関わらず上位175）。"""
        gd = self.governing_date(event_date)
        if gd is None:
            return set()
        top300 = self.per_date[gd]["va_rank_full_list_top300"]
        return {r["code"] for r in top300 if r["rank"] <= 175}


# ---------------------------------------------------------------------------
# /fins/summary（SUE計算用。DBから読み込む）
# ---------------------------------------------------------------------------

import re  # noqa: E402

EVENT_DOCTYPE_RE = re.compile(r"^(1Q|2Q|3Q|FY)FinancialStatements_")
EVENT_CURPERTYPE_OK = {"1Q", "2Q", "3Q", "FY"}


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_fins_summary_all(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT json_blob FROM fins_summary").fetchall()
    return [json.loads(r[0]) for r in rows]


def dedup_same_day(records: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        groups[(r.get("Code", ""), r.get("DiscDate", ""))].append(r)
    out = []
    for _, recs in groups.items():
        recs_sorted = sorted(recs, key=lambda r: (r.get("DiscTime", ""), r.get("DiscNo", "")), reverse=True)
        out.append(recs_sorted[0])
    return out


def build_all_disclosures_index(records: list[dict]) -> dict[str, list[dict]]:
    deduped = dedup_same_day(records)
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in deduped:
        by_code[r.get("Code", "")].append(r)
    for code in by_code:
        by_code[code].sort(key=lambda r: (r.get("DiscDate", ""), r.get("DiscTime", ""), r.get("DiscNo", "")))
    return dict(by_code)


def is_event_disclosure(rec: dict) -> bool:
    return bool(EVENT_DOCTYPE_RE.match(rec.get("DocType", "") or "")) and rec.get("CurPerType") in EVENT_CURPERTYPE_OK


def _event_key(rec: dict) -> dict:
    return {
        "code": rec.get("Code"), "disc_date": rec.get("DiscDate"), "disc_time": rec.get("DiscTime"),
        "disc_no": rec.get("DiscNo"), "doc_type": rec.get("DocType"), "cur_per_type": rec.get("CurPerType"),
    }


def compute_raw_sue_for_code(disclosures: list[dict]) -> list[dict]:
    """spec §3.1〜3.3（PEAD 10年版・旧spec一字一句同一）。"""
    results = []
    for i, rec in enumerate(disclosures):
        if not is_event_disclosure(rec):
            continue
        cur_per_type = rec.get("CurPerType")
        cur_fy_st = rec.get("CurFYSt", "")
        cur_fy_en = rec.get("CurFYEn", "")

        if cur_per_type == "FY":
            a_val = _to_float(rec.get("OdP"))
        else:
            a_val = _to_float(rec.get("FOdP"))

        prev = None
        for j in range(i - 1, -1, -1):
            cand = disclosures[j]
            cand_per_type = cand.get("CurPerType")
            if cand_per_type in ("1Q", "2Q", "3Q") or not EVENT_DOCTYPE_RE.match(cand.get("DocType", "") or ""):
                if cand.get("CurFYSt", "") == cur_fy_st and cand.get("CurFYEn", "") == cur_fy_en:
                    prev = ("current_fy", cand)
                    break
            elif cand_per_type == "FY":
                if cand.get("NxtFYSt", "") == cur_fy_st and cand.get("NxtFYEn", "") == cur_fy_en:
                    prev = ("next_fy_from_prior_fy", cand)
                    break
            continue

        if prev is None:
            results.append({**_event_key(rec), "raw_sue": None, "excluded_reason": "no_matching_prior_disclosure"})
            continue

        prev_kind, prev_rec = prev
        if prev_kind == "current_fy":
            b_val = _to_float(prev_rec.get("FOdP"))
            scale_val = _to_float(prev_rec.get("FSales"))
        else:
            b_val = _to_float(prev_rec.get("NxFOdP"))
            scale_val = _to_float(prev_rec.get("NxFSales"))

        if a_val is None or b_val is None or scale_val is None:
            results.append({**_event_key(rec), "raw_sue": None, "excluded_reason": "null_or_nonnumeric_input"})
            continue
        if scale_val <= 0:
            results.append({**_event_key(rec), "raw_sue": None, "excluded_reason": "scale_le_zero"})
            continue

        raw_sue = (a_val - b_val) / scale_val
        results.append({**_event_key(rec), "raw_sue": raw_sue, "excluded_reason": None,
                         "a_val": a_val, "b_val": b_val, "scale_val": scale_val})
    return results


# ---------------------------------------------------------------------------
# UL/LL パース・約定可否（10Y-COMMON経由でEXP-OBS000001 spec§4.0を継承）
# ---------------------------------------------------------------------------


class AnomalousFlagValueError(ValueError):
    pass


def parse_ul_ll_flag(raw: Any) -> bool | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "1":
        return True
    if s == "0" or s == "":
        return False
    raise AnomalousFlagValueError(f"UL/LL に想定外の値: {raw!r}")


def _floats_equal(x: float, y: float) -> bool:
    return abs(x - y) <= 1e-6 * max(1.0, abs(y))


def buy_blocked(row: dict) -> tuple[bool, str]:
    vo = row.get("vo")
    o = row.get("o")
    if vo is None or float(vo) == 0.0:
        return True, "vo_zero"
    if o is None:
        return True, "o_null"
    ul = parse_ul_ll_flag(row.get("ul"))
    h = row.get("h")
    if ul is True and h is not None and _floats_equal(float(o), float(h)):
        return True, "ul_stop_high_open"
    if ul is True and h is not None and not _floats_equal(float(o), float(h)):
        return False, "ul_hit_but_open_below_high"
    return False, "not_blocked"


def sell_blocked(row: dict) -> tuple[bool, str]:
    vo = row.get("vo")
    o = row.get("o")
    if vo is None or float(vo) == 0.0:
        return True, "vo_zero"
    if o is None:
        return True, "o_null"
    ll = parse_ul_ll_flag(row.get("ll"))
    l_ = row.get("l")
    if ll is True and l_ is not None and _floats_equal(float(o), float(l_)):
        return True, "ll_stop_low_open"
    if ll is True and l_ is not None and not _floats_equal(float(o), float(l_)):
        return False, "ll_hit_but_open_above_low"
    return False, "not_blocked"


# ---------------------------------------------------------------------------
# 統計関数
# ---------------------------------------------------------------------------


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg_rank
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mean_rx, mean_ry = sum(rx) / n, sum(ry) / n
    sxx = sum((x - mean_rx) ** 2 for x in rx)
    syy = sum((y - mean_ry) ** 2 for y in ry)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mean_rx) * (y - mean_ry) for x, y in zip(rx, ry))
    return sxy / math.sqrt(sxx * syy)


def median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def pctile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    idx = min(n - 1, max(0, int(round(p * (n - 1)))))
    return sorted_vals[idx]


def stdev_pop(vals: list[float]) -> float | None:
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


# ---------------------------------------------------------------------------
# 決定的permutation RNG（シード20260915固定）
# ---------------------------------------------------------------------------


def make_rng():
    import random

    return random.Random(PERM_SEED)


# ---------------------------------------------------------------------------
# 配当落ち日構成（EXP-OBS000006用。決済サイクル分岐を含む。10Y-COMMON §6.1）
# ---------------------------------------------------------------------------


def add_months(d: dt.date, months: int) -> dt.date:
    total = d.month - 1 + months
    y = d.year + total // 12
    m = total % 12 + 1
    last_day = calmod.monthrange(y, m)[1]
    day = min(d.day, last_day)
    return dt.date(y, m, day)


def quarter_end(fy_st: dt.date, months: int) -> dt.date:
    return add_months(fy_st, months) - dt.timedelta(days=1)


def settlement_cycle_x_indices(k: int) -> tuple[list[int], str]:
    """10Y-COMMON §6.1: k=idx(b(R))として権利落ち日の添字リストと分岐名を返す。

    呼び出し側が date(b(R)) を判定して cycle を渡す必要があるため、ここでは
    cycle文字列 ('t2'|'t3'|'transition') を受け取る version にする。
    """
    raise NotImplementedError("settlement_cycle_x_indices_for_date を使うこと")


def settlement_cycle_x_indices_for_date(b_r_date: str, k: int) -> tuple[list[int], str]:
    if b_r_date >= "2019-07-22":
        return [k - 1], "t2"
    if b_r_date <= "2019-07-12":
        return [k - 2], "t3"
    return [k - 1, k - 2], "transition"
