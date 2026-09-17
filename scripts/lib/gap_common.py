"""EXP-OBS000003（非決算オーバーナイト・ギャップ）の共通ロジック。

spec: `research/EXP-OBS000003/01-spec.md`。

このモジュールは spec §2.0（T の定義）・§3.1.1（配当落ち日の決定的構成手続き）・
§3.2（ギャップ・σ_gap・z の定義）・§3.4（除外規則 E-1〜E-6）で使う純粋関数を置く。
HTTP通信は含まない。
"""

from __future__ import annotations

import bisect
import calendar
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PEAD_RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"
GAP_RAW_DIR = REPO_ROOT / "data" / "raw" / "gap"
PEAD_RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"
RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000003" / "10-result"

# spec §5.4（PEAD spec §5.4 を参照により取り込み。一字一句そのまま流用）
SCALECAT_U1_OK = {"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"}
MRGN_U2_OK = {"1", "2"}

PRICE_BAND_MAIN = (1000.0, 3500.0)
PRICE_BAND_FALLBACK = (700.0, 8000.0)
LIQUIDITY_MIN_VA = 5e8
UNIVERSE_MAX = 175
UNIVERSE_MIN = 150


# ---------------------------------------------------------------------------
# 基礎データロード
# ---------------------------------------------------------------------------


def load_candidate_codes() -> list[str]:
    d = json.loads((PEAD_RESULT_DIR / "candidate_codes.json").read_text(encoding="utf-8"))
    return d["codes"]


def load_bars_raw(code: str) -> list[dict] | None:
    path = PEAD_RAW_DIR / "bars_daily" / f"{code}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_fins_summary_all() -> list[dict]:
    path = PEAD_RAW_DIR / "fins_summary_all.jsonl"
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


class Calendar:
    """T（参照営業日カレンダー）と銘柄別日次アクセスを提供する（spec §2.0）。"""

    def __init__(self, codes: list[str]):
        self.codes = codes
        self.bars_by_code: dict[str, dict[str, dict]] = {}
        self.missing_bars_codes: list[str] = []
        date_set: set[str] = set()
        for code in codes:
            rows = load_bars_raw(code)
            if rows is None:
                self.missing_bars_codes.append(code)
                continue
            by_date = {}
            for r in rows:
                by_date[r["Date"]] = r
                date_set.add(r["Date"])
            self.bars_by_code[code] = by_date
        self.T: list[str] = sorted(date_set)
        self._index = {d: i + 1 for i, d in enumerate(self.T)}  # 1始まり添字

    def idx(self, date_str: str) -> int | None:
        return self._index.get(date_str)

    def at(self, i: int) -> str | None:
        if 1 <= i <= len(self.T):
            return self.T[i - 1]
        return None

    def row(self, code: str, date_str: str) -> dict | None:
        return self.bars_by_code.get(code, {}).get(date_str)

    def business_day_on_or_before(self, calendar_date: dt.date) -> str | None:
        """T の中で calendar_date 以下である最大の営業日を返す（§3.1.1 step 3 の b(R)）。"""
        s = calendar_date.isoformat()
        pos = bisect.bisect_right(self.T, s)
        if pos == 0:
            return None
        return self.T[pos - 1]


# ---------------------------------------------------------------------------
# §3.1.1 配当落ち日の決定的構成手続き
# ---------------------------------------------------------------------------


def add_months(d: dt.date, months: int) -> dt.date:
    """暦月加算。日を保存し、月末超過のみクランプする（例: 2023-08-21 + 6ヶ月 = 2024-02-21）。"""
    total = d.month - 1 + months
    y = d.year + total // 12
    m = total % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    day = min(d.day, last_day)
    return dt.date(y, m, day)


def quarter_end(fy_st: dt.date, months: int) -> dt.date:
    """`CurFYSt + Nヶ月 - 1日`。"""
    return add_months(fy_st, months) - dt.timedelta(days=1)


def _to_float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date_or_none(s: Any) -> dt.date | None:
    s = (str(s) if s is not None else "").strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


DIV_FIELDS_TO_MONTHS = {"Div1Q": 3, "Div2Q": 6, "Div3Q": 9}


def construct_dividend_basis_records(fins_records: list[dict]) -> tuple[list[dict], int]:
    """spec §3.1.1 step 1・2・4。各 /fins/summary 行から (code, field, basis_date, dps) を作る。

    戻り値: (records, skipped_missing_fy_count)
    """
    out: list[dict] = []
    skipped_missing_fy = 0
    for rec in fins_records:
        code = rec.get("Code")
        fy_st = _parse_date_or_none(rec.get("CurFYSt"))
        fy_en = _parse_date_or_none(rec.get("CurFYEn"))
        if fy_st is None or fy_en is None:
            skipped_missing_fy += 1
            continue
        basis_dates = {
            "Div1Q": quarter_end(fy_st, 3),
            "Div2Q": quarter_end(fy_st, 6),
            "Div3Q": quarter_end(fy_st, 9),
            "DivFY": fy_en,
        }
        for field, basis_date in basis_dates.items():
            val = _to_float_or_none(rec.get(field))
            if val is None:
                continue
            out.append(
                {
                    "code": code,
                    "field": field,
                    "basis_date": basis_date,
                    "dps": val,
                    "is_explicit_zero": val == 0.0,
                    "cur_fy_st": fy_st.isoformat(),
                    "cur_fy_en": fy_en.isoformat(),
                    "disc_date": rec.get("DiscDate"),
                }
            )
    return out, skipped_missing_fy


# spec §3.1.3（EXP-OBS000006 第4版で確定）。日本の株式決済は2019-07-16にT+3からT+2へ移行した。
# 旧spec §3.1.1 step3の「X := b(R)の1営業日前」はT+2を前提とした式（spec原文が明記）であり、
# それ以前の期間には当てはまらない。判定は基準日ではなく権利付最終日（LD。実際に約定する日）が
# 施行日以降かで行う。
SETTLEMENT_T2_EFFECTIVE_DATE = "2019-07-16"


def _rights_offset_x_index(cal: "Calendar", i_b: int) -> int | None:
    """spec §3.1.3: 基準日の直前営業日インデックス i_b（= idx(b(R))）から権利落ち日Xのインデックスを返す。

    LD2 := b(R)の2営業日前（T+2仮定での権利付最終日）
    LD  := LD2 if LD2 >= 2019-07-16（T+2期） else b(R)の3営業日前（T+3期）
    X   := LDの翌営業日 （⇒ T+2期は i_b-1、T+3期は i_b-2）
    """
    if i_b is None or i_b <= 3:
        return None
    ld2_date = cal.at(i_b - 2)
    if ld2_date is not None and ld2_date >= SETTLEMENT_T2_EFFECTIVE_DATE:
        return i_b - 1  # T+2期: Xはb(R)の1営業日前
    return i_b - 2  # T+3期: Xはb(R)の2営業日前


def build_e2a(cal: Calendar, basis_records: list[dict]) -> dict:
    """spec §3.1.1 step 3・5 + §3.1.3。基準日を権利落ち日 X へ写し、DPS>0 の (code,X) を構成する。

    Xのオフセットは決済サイクル依存（§3.1.3。T+2期はb(R)の1営業日前、T+3期は2営業日前）。
    """
    by_code_x_pos: dict[tuple[str, str], list[float]] = defaultdict(list)
    explicit_zero_codes: set[str] = set()
    positive_codes: set[str] = set()
    any_record_codes: set[str] = set()
    no_mapping_count = 0  # b(R) が構成できない（T に R 以下の営業日が無い）
    below_t1_count = 0  # Xのオフセット計算に必要な過去営業日が足りない

    for r in basis_records:
        code = r["code"]
        any_record_codes.add(code)
        b = cal.business_day_on_or_before(r["basis_date"])
        if b is None:
            no_mapping_count += 1
            continue
        i = cal.idx(b)
        x_index = _rights_offset_x_index(cal, i)
        if x_index is None:
            below_t1_count += 1
            continue
        x = cal.at(x_index)
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
        "duplicate_pairs_count": len(duplicate_pairs),
        "duplicate_pairs_examples": [
            {"code": k[0], "x": k[1], "values": v} for k, v in list(duplicate_pairs.items())[:20]
        ],
    }


def build_e2b(cal: Calendar) -> dict[str, str]:
    """spec §3.1.1 の E-2B + §3.1.3。各暦月の最終営業日を基準に定めた X(m) を全銘柄共通で構成する。

    Xのオフセットは決済サイクル依存（§3.1.3。build_e2aと同一規則）。
    戻り値: 暦月 "YYYY-MM" -> X(m) の日付文字列。
    """
    by_month: dict[str, list[str]] = defaultdict(list)
    for d in cal.T:
        by_month[d[:7]].append(d)
    result: dict[str, str] = {}
    for m, days in by_month.items():
        days.sort()
        last_day = days[-1]
        i = cal.idx(last_day)
        x_index = _rights_offset_x_index(cal, i)
        if x_index is None:
            continue
        result[m] = cal.at(x_index)
    return result


# ---------------------------------------------------------------------------
# E-1（分割・併合の権利落ち）
# ---------------------------------------------------------------------------


def is_split_merger_row(row: dict | None) -> bool:
    """spec §3.1.2（EXP-OBS000006 第3版で確定）: 機構ベースの判定。

    `AdjFactor ≠ 1` を直接見る（`ExRT` の値を列挙しない）。
    `ExRT='2'`（株式併合）が10年データで新たに観測されたことを受けて確定した定義。
    根拠: `AdjFactor≠1`の行のExRTはすべて'1'（分割）または'2'（併合）のいずれかであり、
    `ExRT`を明示的に列挙する方式は将来の未知の値を静かに取りこぼす。
    """
    if row is None:
        return False
    af = row.get("AdjFactor")
    return af is not None and abs(af - 1.0) > 1e-9


# ---------------------------------------------------------------------------
# ギャップ g(j,D) の計算（raw O/C を使用。§4.5 の点定義と同一）
# ---------------------------------------------------------------------------


def gap_g(cal: Calendar, code: str, date_str: str) -> float | None:
    i = cal.idx(date_str)
    if i is None or i <= 1:
        return None
    prev_date = cal.at(i - 1)
    row_d = cal.row(code, date_str)
    row_prev = cal.row(code, prev_date)
    if row_d is None or row_prev is None:
        return None
    o = row_d.get("O")
    c = row_prev.get("C")
    if o is None or c is None:
        return None
    o = float(o)
    c = float(c)
    if o <= 0 or c <= 0:
        return None
    return math.log(o / c)


# ---------------------------------------------------------------------------
# 単純OLS
# ---------------------------------------------------------------------------


def ols(xs: list[float], ys: list[float]) -> dict:
    n = len(xs)
    if n < 2:
        return {"a": None, "b": None, "se_b": None, "n": n}
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return {"a": None, "b": None, "se_b": None, "n": n}
    b = sxy / sxx
    a = mean_y - b * mean_x
    if n > 2:
        resid_ss = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
        resid_var = resid_ss / (n - 2)
        se_b = math.sqrt(resid_var / sxx) if sxx > 0 else None
    else:
        se_b = None
    return {"a": a, "b": b, "se_b": se_b, "n": n}


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """タイは平均順位で処理する Spearman 順位相関。"""
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

    rx = ranks(xs)
    ry = ranks(ys)
    fit = ols(rx, ry)
    # Spearman は rank のピアソン相関。b が分散比なので相関係数に変換する。
    if fit["b"] is None:
        return None
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    sxx = sum((x - mean_rx) ** 2 for x in rx)
    syy = sum((y - mean_ry) ** 2 for y in ry)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mean_rx) * (y - mean_ry) for x, y in zip(rx, ry))
    return sxy / math.sqrt(sxx * syy)
