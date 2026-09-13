"""EXP-OBS000001（PEAD）の共通ロジック。spec `research/EXP-OBS000001/01-spec.md` に対応。

このモジュールは spec §3（SUE定義）・§4（約定モデル）で使う純粋関数を置く。
HTTP通信は含まない（`jquants_client.py` の責務）。

9-1（値の対応付け）で確定した対応関係をここに定数として記録する。
根拠は `10-result/params.json` の `field_value_mapping` に出力する実測結果。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"

# --- 9-1 対応付け（実測結果に基づく。params.json に根拠と全ユニーク値を記録する） ---

# §5.4 U-1: TOPIX500相当 = Core30 / Large70 / Mid400
SCALECAT_U1_OK = {"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"}

# §5.4 U-2: 制度信用で買建て可能 = 貸借銘柄(Mrgn=2) + 信用銘柄(Mrgn=1)。
# 'その他'(Mrgn=3) が信用取引不可に相当（MrgnNmラベルが直接対応するTSE標準区分）。
MRGN_U2_OK = {"1", "2"}

# §3.3-3: 予測対象イベント = 四半期決算短信および通期決算短信の開示のみ。
# DocType は "{1Q|2Q|3Q|FY}FinancialStatements_{Consolidated|NonConsolidated}_{JP|IFRS|US}" 系。
EVENT_DOCTYPE_RE = re.compile(r"^(1Q|2Q|3Q|FY)FinancialStatements_")
# CurPerType はイベント種別と対応（四半期=1Q/2Q/3Q、通期=FY）
EVENT_CURPERTYPE_OK = {"1Q", "2Q", "3Q", "FY"}


def load_fins_summary(codes_filter: set[str] | None = None) -> list[dict]:
    """`fins_summary_all.jsonl` を読み込む。codes_filter が与えられればその銘柄のみ返す。"""
    path = RAW_DIR / "fins_summary_all.jsonl"
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if codes_filter is not None and rec.get("Code") not in codes_filter:
                continue
            records.append(rec)
    return records


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


def dedup_same_day(records: list[dict]) -> list[dict]:
    """§3.3-5: 同一銘柄・同一DiscDateに複数開示がある場合、DiscTime最遅・同時刻ならDiscNo最大の1件のみ採用。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        groups[(r.get("Code", ""), r.get("DiscDate", ""))].append(r)
    out = []
    for _, recs in groups.items():
        recs_sorted = sorted(
            recs, key=lambda r: (r.get("DiscTime", ""), r.get("DiscNo", "")), reverse=True
        )
        out.append(recs_sorted[0])
    return out


def build_all_disclosures_index(records: list[dict]) -> dict[str, list[dict]]:
    """銘柄コードごとに (DiscDate, DiscTime, DiscNo) 昇順でソートした開示リストを作る。

    §3.3-5 の重複排除は「同一銘柄・同一DiscDate」単位で行うため、まず全レコードに適用してから
    銘柄別に整理する。
    """
    deduped = dedup_same_day(records)
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in deduped:
        by_code[r.get("Code", "")].append(r)
    for code in by_code:
        by_code[code].sort(key=lambda r: (r.get("DiscDate", ""), r.get("DiscTime", ""), r.get("DiscNo", "")))
    return dict(by_code)


def is_event_disclosure(rec: dict) -> bool:
    return bool(EVENT_DOCTYPE_RE.match(rec.get("DocType", "") or "")) and rec.get(
        "CurPerType"
    ) in EVENT_CURPERTYPE_OK


def compute_raw_sue_for_code(disclosures: list[dict]) -> list[dict]:
    """1銘柄分の開示列（DiscDate昇順）から、イベントごとのRawSUEを計算する。

    spec §3.1〜3.3 に厳密に対応。除外されたイベントも `excluded_reason` 付きで返す
    （除外件数の記録に使うため、リストからは消さずに理由を付与する）。
    """
    results = []
    for i, rec in enumerate(disclosures):
        if not is_event_disclosure(rec):
            continue
        cur_per_type = rec.get("CurPerType")
        cur_fy_st = rec.get("CurFYSt", "")
        cur_fy_en = rec.get("CurFYEn", "")

        # A(t): 1Q/2Q/3Q開示 -> FOdP。FY開示 -> OdP（実績）。
        if cur_per_type == "FY":
            a_val = _to_float(rec.get("OdP"))
        else:
            a_val = _to_float(rec.get("FOdP"))

        # 直前開示（同一銘柄で本開示より前の全開示のうち直近のもの）を探す。
        # B(t)/Scale(t) は「同一会計年度」の一致判定が必要。
        prev = None
        for j in range(i - 1, -1, -1):
            cand = disclosures[j]
            cand_per_type = cand.get("CurPerType")
            if cand_per_type in ("1Q", "2Q", "3Q") or not EVENT_DOCTYPE_RE.match(
                cand.get("DocType", "") or ""
            ):
                # 1Q/2Q/3Q開示 または 業績修正等の非イベント開示 -> CurFYSt/CurFYEnの完全一致を要求
                if cand.get("CurFYSt", "") == cur_fy_st and cand.get("CurFYEn", "") == cur_fy_en:
                    prev = ("current_fy", cand)
                    break
            elif cand_per_type == "FY":
                # 前期のFY開示 -> NxtFYSt/NxtFYEnが本開示のCurFYSt/CurFYEnと一致するか
                if cand.get("NxtFYSt", "") == cur_fy_st and cand.get("NxtFYEn", "") == cur_fy_en:
                    prev = ("next_fy_from_prior_fy", cand)
                    break
            # 一致しなければ、さらに過去に遡って探索を続ける（直近の会計年度一致開示を探すため）
            continue

        if prev is None:
            results.append(
                {**_event_key(rec), "raw_sue": None, "excluded_reason": "no_matching_prior_disclosure"}
            )
            continue

        prev_kind, prev_rec = prev
        if prev_kind == "current_fy":
            b_val = _to_float(prev_rec.get("FOdP"))
            scale_val = _to_float(prev_rec.get("FSales"))
        else:  # next_fy_from_prior_fy
            b_val = _to_float(prev_rec.get("NxFOdP"))
            scale_val = _to_float(prev_rec.get("NxFSales"))

        if a_val is None or b_val is None or scale_val is None:
            results.append(
                {**_event_key(rec), "raw_sue": None, "excluded_reason": "null_or_nonnumeric_input"}
            )
            continue
        if scale_val <= 0:
            results.append({**_event_key(rec), "raw_sue": None, "excluded_reason": "scale_le_zero"})
            continue

        raw_sue = (a_val - b_val) / scale_val
        results.append(
            {
                **_event_key(rec),
                "raw_sue": raw_sue,
                "excluded_reason": None,
                "a_val": a_val,
                "b_val": b_val,
                "scale_val": scale_val,
            }
        )
    return results


def _event_key(rec: dict) -> dict:
    return {
        "code": rec.get("Code"),
        "disc_date": rec.get("DiscDate"),
        "disc_time": rec.get("DiscTime"),
        "disc_no": rec.get("DiscNo"),
        "doc_type": rec.get("DocType"),
        "cur_per_type": rec.get("CurPerType"),
    }


def business_days_between(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


# --- §4.0（2026-09-13改訂）: UL/LL フラグの解釈と約定可否判定 ---


class AnomalousFlagValueError(ValueError):
    """UL/LL が '1'/'0'/''/null 以外の値を取った場合。推測で埋めずここで検出し、呼び出し側でSへ差し戻す。"""


def parse_ul_ll_flag(raw: Any) -> bool | None:
    """§4.0.3 のパース規則。

    戻り値: True(=1) / False(=0またはnull相当) / 例外(想定外の値)。
    None（欠損）は呼び出し側で別途カウントする（本関数はNoneをFalse同様に扱わない）。
    """
    if raw is None:
        return None  # 欠損。呼び出し側で欠損件数として記録すること。
    s = str(raw).strip()
    if s == "1":
        return True
    if s == "0" or s == "":
        return False
    raise AnomalousFlagValueError(f"UL/LL に想定外の値: {raw!r}")


def _floats_equal(x: float, y: float) -> bool:
    return abs(x - y) <= 1e-6 * max(1.0, abs(y))


def buy_blocked(row: dict) -> tuple[bool, str]:
    """§4.0.4 BUY_BLOCKED(t)。戻り値: (真偽, 内訳理由コード)。

    理由コード: 'vo_zero' / 'o_null' / 'ul_stop_high_open' / 'not_blocked' /
                'ul_hit_but_open_below_high'（参考値：約定したとみなした側）
    """
    vo = row.get("Vo")
    o = row.get("O")
    if vo is None or float(vo) == 0.0:
        return True, "vo_zero"
    if o is None:
        return True, "o_null"
    ul = parse_ul_ll_flag(row.get("UL"))
    h = row.get("H")
    if ul is True and h is not None and _floats_equal(float(o), float(h)):
        return True, "ul_stop_high_open"
    if ul is True and h is not None and not _floats_equal(float(o), float(h)):
        return False, "ul_hit_but_open_below_high"
    return False, "not_blocked"


def sell_blocked(row: dict) -> tuple[bool, str]:
    """§4.0.4 SELL_BLOCKED(t)。戻り値: (真偽, 内訳理由コード)。"""
    vo = row.get("Vo")
    o = row.get("O")
    if vo is None or float(vo) == 0.0:
        return True, "vo_zero"
    if o is None:
        return True, "o_null"
    ll = parse_ul_ll_flag(row.get("LL"))
    l_ = row.get("L")
    if ll is True and l_ is not None and _floats_equal(float(o), float(l_)):
        return True, "ll_stop_low_open"
    if ll is True and l_ is not None and not _floats_equal(float(o), float(l_)):
        return False, "ll_hit_but_open_above_low"
    return False, "not_blocked"
