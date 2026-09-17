"""EXP-OBS000003 の合成ロジック（`gap_common.py` の純粋関数を組み立てる）。

spec: `research/EXP-OBS000003/01-spec.md` §3.2〜§3.6・§5.4・§6.0。
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import gap_common as gc
from . import pead_common as pc

REPO_ROOT = gc.REPO_ROOT
GAP_RAW_DIR = gc.GAP_RAW_DIR
PEAD_RAW_DIR = gc.PEAD_RAW_DIR


# ---------------------------------------------------------------------------
# 有効ギャップ・除外集合の構築（§3.1.1・§3.2）
# ---------------------------------------------------------------------------


def build_valid_positions(
    cal: gc.Calendar, e2a_set: set[tuple[str, str]], e2b_date_set: set[str]
) -> dict[str, list[int]]:
    """各銘柄について、有効ギャップ（E-1/E-2A/E-2B の除外日でない）が存在する位置 i の昇順リストを返す。"""
    valid_positions: dict[str, list[int]] = {}
    for code in cal.codes:
        if code not in cal.bars_by_code:
            continue
        positions: list[int] = []
        for i in range(2, len(cal.T) + 1):
            date_i = cal.at(i)
            date_prev = cal.at(i - 1)
            row_i = cal.row(code, date_i)
            row_prev = cal.row(code, date_prev)
            if row_i is None or row_prev is None:
                continue
            o = row_i.get("O")
            c = row_prev.get("C")
            if o is None or c is None:
                continue
            o = float(o)
            c = float(c)
            if o <= 0 or c <= 0:
                continue
            if gc.is_split_merger_row(row_i):
                continue
            if (code, date_i) in e2a_set:
                continue
            if date_i in e2b_date_set:
                continue
            positions.append(i)
        valid_positions[code] = positions
    return valid_positions


def compute_k_distribution(
    cal: gc.Calendar, valid_positions: dict[str, list[int]]
) -> dict:
    """spec §3.2「遡り上限66の根拠」・§9-5: 60本収集に要した遡り営業日数Kの分布（uncapped）。

    評価対象は「その銘柄が実際にその日足行を持つ日」に限る（上場前・上場廃止後・
    データ範囲外の日を「銘柄日」として数えると、データが存在しないだけの
    見かけ上の大きなKが混入するため）。
    """
    all_k: list[int] = []
    insufficient = 0
    attempted = 0
    for code, positions in valid_positions.items():
        present_days = cal.bars_by_code.get(code, {})
        for i in range(2, len(cal.T) + 1):
            if cal.at(i) not in present_days:
                continue  # その銘柄がその日に上場・取引していない（銘柄日として数えない）
            attempted += 1
            cnt = bisect.bisect_right(positions, i - 1)
            if cnt < 60:
                insufficient += 1
                continue
            pos_60th_back = positions[cnt - 60]
            all_k.append(i - pos_60th_back)
    all_k.sort()
    n = len(all_k)

    def pct(p: float) -> float | None:
        if n == 0:
            return None
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return all_k[idx]

    max_k = all_k[-1] if all_k else None
    cover_66 = (sum(1 for k in all_k if k <= 66) / n) if n else None
    return {
        "attempted_code_day_pairs": attempted,
        "insufficient_history_count": insufficient,
        "k_collected_count": n,
        "k_max": max_k,
        "k_p50": pct(0.50),
        "k_p90": pct(0.90),
        "k_p99": pct(0.99),
        "k_le_66_coverage_rate": cover_66,
        "gate_k_le_66_all": (max_k is not None and max_k <= 66),
    }


def build_gap_value_cache(cal: gc.Calendar, valid_positions: dict[str, list[int]]) -> dict[str, dict[int, float]]:
    cache: dict[str, dict[int, float]] = {}
    for code, positions in valid_positions.items():
        d: dict[int, float] = {}
        for i in positions:
            g = gc.gap_g(cal, code, cal.at(i))
            if g is not None:
                d[i] = g
        cache[code] = d
    return cache


def sigma_gap(
    cal: gc.Calendar,
    code: str,
    i: int,
    valid_positions: dict[str, list[int]],
    gap_cache: dict[str, dict[int, float]],
) -> tuple[float | None, int, int | None]:
    """spec §3.2 σ_gap(j,D)。戻り値: (sigma, 窓内で見つかった有効ギャップ本数, K)。"""
    positions = valid_positions.get(code, [])
    lo = i - 66
    hi_idx = bisect.bisect_right(positions, i - 1)
    lo_idx = bisect.bisect_left(positions, lo)
    window = positions[lo_idx:hi_idx]
    if len(window) < 60:
        return None, len(window), None
    chosen = window[-60:]
    k = i - chosen[0]
    gaps = [gap_cache[code][p] for p in chosen]
    n = len(gaps)
    mean = sum(gaps) / n
    var = sum((x - mean) ** 2 for x in gaps) / (n - 1)
    return math.sqrt(var), len(window), k


# ---------------------------------------------------------------------------
# ユニバース構築（§5.4。PEAD spec §5.4 U-1〜U-7 を一字一句そのまま流用）
# ---------------------------------------------------------------------------


def load_master(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))["data"]
    return {r["Code"]: r for r in data}


def trailing_median_va(bars: list[dict], as_of: dt.date, window: int = 60) -> float | None:
    prior = [r for r in bars if dt.date.fromisoformat(r["Date"]) < as_of]
    prior.sort(key=lambda r: r["Date"])
    window_rows = prior[-window:]
    if not window_rows:
        return None
    vas = sorted(float(r["Va"]) for r in window_rows if r.get("Va") is not None)
    if not vas:
        return None
    n = len(vas)
    mid = n // 2
    if n % 2 == 1:
        return vas[mid]
    return (vas[mid - 1] + vas[mid]) / 2.0


def price_at_or_before(bars: list[dict], as_of: dt.date) -> tuple[float | None, str | None]:
    eligible = [r for r in bars if dt.date.fromisoformat(r["Date"]) <= as_of]
    if not eligible:
        return None, None
    eligible.sort(key=lambda r: r["Date"])
    last = eligible[-1]
    return float(last["C"]), last["Date"]


def build_period_universe(
    period_label: str, period_start: dt.date, master_path: Path, candidate_codes: list[str], log: list[str]
) -> dict:
    master = load_master(master_path)
    rows = []
    missing_bars = []
    for code in candidate_codes:
        m = master.get(code)
        if m is None:
            continue
        u1 = m.get("ScaleCat") in gc.SCALECAT_U1_OK
        u2 = m.get("Mrgn") in gc.MRGN_U2_OK
        if not (u1 and u2):
            continue
        bars = gc.load_bars_raw(code)
        if bars is None:
            missing_bars.append(code)
            continue
        va_median = trailing_median_va(bars, period_start, window=60)
        price, price_date = price_at_or_before(bars, period_start)
        rows.append(
            {
                "code": code,
                "co_name": m.get("CoName"),
                "scale_cat": m.get("ScaleCat"),
                "mrgn": m.get("Mrgn"),
                "s33": m.get("S33"),
                "s33_nm": m.get("S33Nm"),
                "va_median_60bd": va_median,
                "price": price,
                "price_date": price_date,
            }
        )

    def apply_filters(price_band: tuple[float, float]) -> list[dict]:
        out = []
        for r in rows:
            if r["va_median_60bd"] is None or r["va_median_60bd"] < gc.LIQUIDITY_MIN_VA:
                continue
            if r["price"] is None or not (price_band[0] <= r["price"] <= price_band[1]):
                continue
            out.append(r)
        return out

    passed_main = apply_filters(gc.PRICE_BAND_MAIN)
    fallback_applied = False
    passed = passed_main
    if len(passed_main) < gc.UNIVERSE_MIN:
        fallback_applied = True
        passed = apply_filters(gc.PRICE_BAND_FALLBACK)

    unresolved = len(passed) < gc.UNIVERSE_MIN
    truncated = False
    if len(passed) > gc.UNIVERSE_MAX:
        passed = sorted(passed, key=lambda r: -r["va_median_60bd"])[: gc.UNIVERSE_MAX]
        truncated = True

    log.append(
        f"[{period_label}] candidates={len(candidate_codes)} u1u2_pass={len(rows)} "
        f"missing_bars={len(missing_bars)} main_band_pass={len(passed_main)} "
        f"fallback_applied={fallback_applied} final_count={len(passed)} truncated={truncated} "
        f"unresolved(<150)={unresolved}"
    )

    return {
        "period": period_label,
        "period_start": period_start.isoformat(),
        "candidate_count": len(candidate_codes),
        "u1_u2_pass_count": len(rows),
        "missing_bars_codes": missing_bars,
        "price_band_main": list(gc.PRICE_BAND_MAIN),
        "price_band_fallback_applied": fallback_applied,
        "price_band_fallback": list(gc.PRICE_BAND_FALLBACK) if fallback_applied else None,
        "main_band_pass_count": len(passed_main),
        "final_count": len(passed),
        "truncated_to_max": truncated,
        "unresolved_below_min": unresolved,
        "codes": [r["code"] for r in passed],
        "details": passed,
    }


# ---------------------------------------------------------------------------
# 決算開示日インデックス（E-3・§3.5・§3.7）
# ---------------------------------------------------------------------------


def build_disc_dates_by_code(fins_records: list[dict]) -> dict[str, set[str]]:
    by_code: dict[str, set[str]] = defaultdict(set)
    for r in fins_records:
        code = r.get("Code")
        disc = r.get("DiscDate")
        if code and disc:
            by_code[code].add(disc)
    return dict(by_code)


def build_disc_positions_by_code(cal: gc.Calendar, disc_dates_by_code: dict[str, set[str]]) -> dict[str, set[int]]:
    """DiscDate（暦日）を T の位置へ写す（E-3・§3.5）。T に直接存在すればその位置、
    存在しない場合は on-or-before の営業日の位置を使う（開示日が非営業日にずれる稀なケース）。
    """
    out: dict[str, set[int]] = {}
    for code, dates in disc_dates_by_code.items():
        positions = set()
        for d in dates:
            i = cal.idx(d)
            if i is None:
                try:
                    dd = dt.date.fromisoformat(d)
                except ValueError:
                    continue
                b = cal.business_day_on_or_before(dd)
                if b is None:
                    continue
                i = cal.idx(b)
            if i is not None:
                positions.add(i)
        out[code] = positions
    return out


# ---------------------------------------------------------------------------
# 候補判定エンジン（spec §3.4 C-1〜C-4・E-1〜E-6）
# ---------------------------------------------------------------------------


def evaluate_candidate_row(
    cal: gc.Calendar,
    code: str,
    i: int,
    valid_positions: dict[str, list[int]],
    gap_cache: dict[str, dict[int, float]],
    e2a_set: set[tuple[str, str]],
    e2b_date_set: set[str],
    disc_positions_by_code: dict[str, set[int]],
) -> dict:
    date_i = cal.at(i)
    date_prev = cal.at(i - 1)
    row_i = cal.row(code, date_i)
    row_prev = cal.row(code, date_prev)

    exclusions: list[str] = []

    own_day_ok = False
    g = None
    if row_i is not None and row_prev is not None:
        o = row_i.get("O")
        c = row_prev.get("C")
        if o is not None and c is not None and float(o) > 0 and float(c) > 0:
            own_day_ok = True
            g = math.log(float(o) / float(c))

    if not own_day_ok:
        return {
            "code": code, "i": i, "date": date_i, "own_day_ok": False,
            "exclusions": ["missing_data"], "is_candidate": False,
            "g": None, "sigma": None, "z": None, "S": None, "window_count": None, "k": None,
        }

    if gc.is_split_merger_row(row_i):
        exclusions.append("E1")
    if (code, date_i) in e2a_set:
        exclusions.append("E2A")
    if date_i in e2b_date_set:
        exclusions.append("E2B")
    disc_positions = disc_positions_by_code.get(code, set())
    if any((i + off) in disc_positions for off in (-2, -1, 0, 1, 2)):
        exclusions.append("E3")
    ul_prev = pc.parse_ul_ll_flag(row_prev.get("UL"))
    ll_prev = pc.parse_ul_ll_flag(row_prev.get("LL"))
    if ul_prev is True or ll_prev is True:
        exclusions.append("E4")
    ll_d = pc.parse_ul_ll_flag(row_i.get("LL"))
    l_d = row_i.get("L")
    o_d = row_i.get("O")
    if ll_d is True and l_d is not None and o_d is not None and abs(float(o_d) - float(l_d)) <= 1e-6 * max(1.0, abs(float(l_d))):
        exclusions.append("E5")
    if g >= 0:
        exclusions.append("E6")

    sigma, window_count, k = sigma_gap(cal, code, i, valid_positions, gap_cache)
    if sigma is None:
        exclusions.append("insufficient_window")
    elif sigma <= 0 or not math.isfinite(sigma):
        exclusions.append("zero_or_nonfinite_sigma")

    z = (g / sigma) if (sigma is not None and sigma > 0 and math.isfinite(sigma)) else None
    s_val = (-z) if z is not None else None

    is_candidate = len(exclusions) == 0
    return {
        "code": code, "i": i, "date": date_i, "own_day_ok": True,
        "exclusions": exclusions, "is_candidate": is_candidate,
        "g": g, "sigma": sigma, "z": z, "S": s_val,
        "window_count": window_count, "k": k,
    }


def build_period_rows(
    cal: gc.Calendar,
    codes_universe: list[str],
    i_start: int,
    i_end: int,
    valid_positions: dict[str, list[int]],
    gap_cache: dict[str, dict[int, float]],
    e2a_set: set[tuple[str, str]],
    e2b_date_set: set[str],
    disc_positions_by_code: dict[str, set[int]],
) -> list[dict]:
    rows = []
    for code in codes_universe:
        if code not in cal.bars_by_code:
            continue
        for i in range(i_start, i_end + 1):
            date_i = cal.at(i)
            if date_i not in cal.bars_by_code[code]:
                continue  # その銘柄がその日に上場・取引していない
            rows.append(
                evaluate_candidate_row(
                    cal, code, i, valid_positions, gap_cache, e2a_set, e2b_date_set, disc_positions_by_code
                )
            )
    return rows
