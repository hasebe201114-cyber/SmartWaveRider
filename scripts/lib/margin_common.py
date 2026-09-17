"""EXP-OBS000008（信用倍率単独の方向予測力検定）の共通ロジック。

spec: `research/EXP-OBS000008/01-spec.md` §2〜§4。

このモジュールは spec §2.0.2（週次カレンダー`W`）・§3（ΔM_w(j)の定義・除外規則E-1〜E-4）で
使う純粋関数を置く。HTTP通信は行わない。`data/raw/margin_interest/` の読み取りのみ。

**既存キャッシュを一切書き換えない（read-only）。**
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MARGIN_RAW_DIR = REPO_ROOT / "data" / "raw" / "margin_interest"

# spec §5.2 U-2 注記・9-4実測: MrgnNm の値は {'信用'(Mrgn=1), '貸借'(Mrgn=2), 'その他'(Mrgn=3)}
# の3値に限られる（EXP-OBS000005 params.json field_value_mapping で確認済み）。
# '貸借'(2)はJPX/証券会社の実務上「貸借銘柄」＝証券金融会社からの株券貸借を通じて
# 制度信用売り（空売り）が可能な銘柄区分であり、'信用'(1)は買建てのみ可能な区分
# （新規の空売りに必要な貸株の仕組みを持たない）。'その他'(3)は信用取引不可。
# これはMrgn/MrgnNmフィールドの標準的な値集合であり、一意に定まる（推測ではない）。
MRGN_SHORT_ELIGIBLE_OK = {"2"}  # 貸借銘柄のみ（保守的近似・spec §5.2注記どおり）

GAP_DAYS_MAX = 21  # E-3


def load_margin_raw(code: str) -> list[dict] | None:
    path = MARGIN_RAW_DIR / f"{code}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_code_canonical_series(records: list[dict]) -> tuple[list[dict], int, int]:
    """E-4: 同一Code・同一Dateの重複は「配列内の後方の要素を採用」。

    戻り値: (Date昇順の正規化済みレコード列, 重複が存在したDateの数, 重複により捨てられた件数)
    """
    last_idx_by_date: dict[str, int] = {}
    for idx, r in enumerate(records):
        last_idx_by_date[r["Date"]] = idx  # 後方の要素で上書きされる＝後勝ち
    date_counts: dict[str, int] = {}
    for r in records:
        date_counts[r["Date"]] = date_counts.get(r["Date"], 0) + 1
    dup_dates = [d for d, c in date_counts.items() if c > 1]
    dropped = sum(c - 1 for c in date_counts.values() if c > 1)
    canonical = [records[last_idx_by_date[d]] for d in sorted(last_idx_by_date.keys())]
    return canonical, len(dup_dates), dropped


def compute_delta_m_series(canonical_sorted: list[dict]) -> list[dict]:
    """spec §3.1〜3.2: M_w(j)・ΔM_w(j)を、直前の**配列上隣接**する観測との対で計算する。

    w'は「銘柄j自身の直前の有効観測」＝配列上直前のレコード（`00-prescreen.md` §4.3・
    §9.3実測 = `margin_prescreen_counts.py`のconsecutive_pairsロジックと同一の定義）。
    さらに遡って有効な観測を探すことはしない（E-2はこの直前レコードのShrtVol=0のみを見る）。
    """
    out: list[dict] = []
    for i, rec in enumerate(canonical_sorted):
        date = rec["Date"]
        shrt = rec.get("ShrtVol")
        longv = rec.get("LongVol")
        shrt_zero_or_null = shrt is None or shrt == 0
        m_level = None if shrt_zero_or_null or longv is None else (float(longv) / float(shrt))
        entry: dict[str, Any] = {
            "date": date,
            "shrt_vol": shrt,
            "long_vol": longv,
            "m_level": m_level,
            "m_level_valid": m_level is not None,
        }
        if i == 0:
            entry.update(
                delta_m=None,
                delta_m_valid=False,
                exclusion_reasons=["no_previous_observation"],
                gap_days=None,
            )
            out.append(entry)
            continue

        prev = canonical_sorted[i - 1]
        prev_shrt = prev.get("ShrtVol")
        prev_long = prev.get("LongVol")
        prev_shrt_zero_or_null = prev_shrt is None or prev_shrt == 0
        gap_days = (dt.date.fromisoformat(date) - dt.date.fromisoformat(prev["Date"])).days

        reasons: list[str] = []
        if shrt_zero_or_null:
            reasons.append("E-1_current_shrtvol_zero_or_null")
        if prev_shrt_zero_or_null:
            reasons.append("E-2_prev_shrtvol_zero_or_null")
        if gap_days > GAP_DAYS_MAX:
            reasons.append("E-3_gap_days_gt_21")

        if reasons:
            entry.update(delta_m=None, delta_m_valid=False, exclusion_reasons=reasons, gap_days=gap_days)
        else:
            m_prev = float(prev_long) / float(prev_shrt)
            delta_m = m_level - m_prev
            entry.update(delta_m=delta_m, delta_m_valid=True, exclusion_reasons=[], gap_days=gap_days)
        out.append(entry)
    return out


def build_all_series(codes: list[str]) -> dict[str, dict]:
    """候補銘柄codesすべてについて正規化・ΔM系列を構築する。

    戻り値: {code: {"canonical": [...], "series": [...], "dup_dates_count": int,
                     "dup_dropped_count": int, "missing": bool}}
    """
    out: dict[str, dict] = {}
    for code in codes:
        raw = load_margin_raw(code)
        if raw is None:
            out[code] = {"missing": True}
            continue
        canonical, dup_dates_count, dup_dropped = build_code_canonical_series(raw)
        series = compute_delta_m_series(canonical)
        out[code] = {
            "missing": False,
            "canonical": canonical,
            "series": series,
            "dup_dates_count": dup_dates_count,
            "dup_dropped_count": dup_dropped,
        }
    return out


def build_weekly_calendar(all_series: dict[str, dict]) -> list[str]:
    """spec §2.0.2: Wは候補集合のDateの和集合を昇順に並べた列。"""
    date_set: set[str] = set()
    for info in all_series.values():
        if info.get("missing"):
            continue
        for entry in info["series"]:
            date_set.add(entry["date"])
    return sorted(date_set)


def index_delta_m_by_date(all_series: dict[str, dict]) -> dict[str, dict[str, dict]]:
    """date -> {code: entry(dict)} のインデックスを作る（entryはdelta_m_valid問わず全件含む）。"""
    by_date: dict[str, dict[str, dict]] = {}
    for code, info in all_series.items():
        if info.get("missing"):
            continue
        for entry in info["series"]:
            by_date.setdefault(entry["date"], {})[code] = entry
    return by_date
