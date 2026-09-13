#!/usr/bin/env python3
"""EXP-OBS000001 SUE-P / SUE-R イベント抽出。spec §3 の実装。

`/fins/summary` の生レコード（銘柄コード単位の全開示履歴。訂正開示・本決算を
含む「生の」履歴でなければならない — §3.1手順3の「直前の開示」を正しく
探すために全件が要る）を入力に取り、spec §3 の手順どおり決定的にイベントを
抽出する。

**実データを見ないと確定できない前提について**: `DocType`（決算短信に相当する
値）と `CurPerType`（四半期区分の表記）の実際の値は未確認である。本モジュールは
既定のキーワード/パターンで自動判定を試みるが、**1件もマッチしない場合や
未知の表記に遭遇した場合は憶測で処理を続けず `UnconfirmedFieldValueError` を
送出する**（spec §9.3「spec の矛盾・曖昧さを自分の解釈で埋めない」）。
呼び出し側は例外メッセージに含まれる実測ユニーク値を見て、
S戦略チームへの差し戻しを検討すること。

このモジュール自体は API を呼ばない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 決算短信に相当するとみなす DocType のキーワード（大文字小文字を無視した部分一致）。
# 実データのユニーク値がこれに1つも一致しない場合は例外を送出し、
# 実測値を全て提示する（憶測で全件通過させない）。
DOCTYPE_TANSHIN_KEYWORDS = (
    "決算短信",
    "tanshin",
    "financialstatements",
    "kessan",
    "summary",
)

# 四半期区分の代表的な表記パターン。1Q/2Q/3Q/FY(4Q) 以外に遭遇したら例外にする。
_QUARTER_PATTERNS: dict[str, int] = {
    "1q": 1,
    "q1": 1,
    "2q": 2,
    "q2": 2,
    "3q": 3,
    "q3": 3,
}
_FY_PATTERNS = {"fy", "4q", "q4", "本決算", "通期"}


class UnconfirmedFieldValueError(RuntimeError):
    """`DocType`/`CurPerType` 等、実データで確認が必要なフィールド値が未知だったときに送出する。"""


@dataclass
class EventExclusionCounts:
    total_raw_disclosures: int = 0
    excluded_doctype_not_tanshin: int = 0
    excluded_fy_quarter: int = 0
    excluded_correction_duplicate: int = 0
    excluded_no_prior_disclosure: int = 0
    excluded_fop_prev_non_positive: int = 0
    excluded_both_op_missing: int = 0
    n_events_kept: int = 0
    used_ncop_fallback: int = 0
    used_ncta_fallback: int = 0


@dataclass
class PeadEvent:
    code: str
    disc_no: str
    disc_date: str
    disc_time: str | None
    cur_fy_en: str
    cur_fy_st: str
    cur_per_en: str
    cur_per_type_raw: str
    quarter: int  # 1, 2, 3
    op_used_field: str  # "OP" or "NCOP"
    op_value: float
    fop_prev: float
    fop_prev_disc_date: str
    fop_prev_used_field: str  # "FOP" or "FNCOP"
    progress_ratio: float  # P_i,t
    raw_sue: float  # RawSUE_i,t = P - q/4
    fop_t: float | None  # 今回開示の改訂後予想（SUE-Rに使う。SUE-Pには使わない）
    ta_t: float | None
    ta_used_field: str | None


def _match_doctype(doctype: str) -> bool:
    text = doctype.lower()
    return any(kw.lower() in text for kw in DOCTYPE_TANSHIN_KEYWORDS)


def enumerate_doctypes(records: list[dict]) -> list[str]:
    return sorted({str(r.get("DocType", "")) for r in records if r.get("DocType") is not None})


def assert_doctype_recognizable(unique_doctypes: list[str]) -> None:
    matched = [d for d in unique_doctypes if _match_doctype(d)]
    if not matched:
        raise UnconfirmedFieldValueError(
            "`DocType` の実データに『決算短信』に相当すると判定できる値が"
            f"1件も見つからなかった。観測されたユニーク値: {unique_doctypes}。"
            "spec §3.1『DocTypeのユニーク値を全列挙し、決算短信に相当するものだけを採る』"
            "を満たせない。独自判断でキーワードを追加せず、S戦略チームへ差し戻すこと。"
        )


def _parse_quarter(cur_per_type_raw: str) -> int | None:
    text = str(cur_per_type_raw).strip().lower()
    if text in _FY_PATTERNS:
        return 4
    if text in _QUARTER_PATTERNS:
        return _QUARTER_PATTERNS[text]
    # 数値1〜4のみの表記（例: "1", "2"）
    if text in {"1", "2", "3"}:
        return int(text)
    if text == "4":
        return 4
    return None


def assert_quarter_values_recognizable(unique_values: list[str]) -> None:
    unknown = [v for v in unique_values if _parse_quarter(v) is None]
    if unknown:
        raise UnconfirmedFieldValueError(
            "`CurPerType` の実データに未知の表記が含まれる。"
            f"未知の値: {unknown}（既知として扱えるのは 1Q/2Q/3Q/FY 相当の表記のみ）。"
            "spec §3.1 手順6 の四半期区分抽出を確定できない。"
            "独自判断で割り当てず、S戦略チームへ差し戻すこと。"
        )


def _num(rec: dict, field_name: str) -> float | None:
    v = rec.get(field_name)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_events_for_code(
    code: str, raw_records: list[dict], counts: EventExclusionCounts
) -> list[PeadEvent]:
    """1銘柄分の `/fins/summary` 生レコード（全開示履歴）からイベントを抽出する。

    `raw_records` は当該銘柄の**全開示履歴**（訂正・本決算を含む）でなければ
    ならない。§3.1手順3「直前の開示」の探索に全履歴が必要なため。
    """
    counts.total_raw_disclosures += len(raw_records)
    recs_sorted = sorted(raw_records, key=lambda r: (str(r.get("DiscDate", "")), str(r.get("DiscNo", ""))))

    # (CurFYEn, CurPerType) ごとに DiscNo 最小（最初の開示）だけを候補とする。
    # 決算短信以外の DocType は候補から除外する。
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in recs_sorted:
        doctype = str(r.get("DocType", ""))
        if not _match_doctype(doctype):
            counts.excluded_doctype_not_tanshin += 1
            continue
        key = (str(r.get("CurFYEn", "")), str(r.get("CurPerType", "")))
        groups.setdefault(key, []).append(r)

    events: list[PeadEvent] = []
    for (cur_fy_en, cur_per_type_raw), recs in groups.items():
        recs.sort(key=lambda r: str(r.get("DiscNo", "")))
        primary = recs[0]
        counts.excluded_correction_duplicate += len(recs) - 1

        quarter = _parse_quarter(cur_per_type_raw)
        if quarter is None:
            # assert_quarter_values_recognizable を先に呼んでいれば通常到達しない防御。
            raise UnconfirmedFieldValueError(
                f"code={code}: CurPerType='{cur_per_type_raw}' を解釈できない。"
            )
        if quarter == 4:
            counts.excluded_fy_quarter += 1
            continue

        op_value = _num(primary, "OP")
        op_field = "OP"
        if op_value is None:
            op_value = _num(primary, "NCOP")
            op_field = "NCOP"
            counts.used_ncop_fallback += 1
        if op_value is None:
            counts.excluded_both_op_missing += 1
            continue

        disc_date_t = str(primary.get("DiscDate", ""))

        # 直前の開示（同一 CurFYEn・DiscDate < t で最大）を全履歴から探す。
        prior_candidates = [
            r
            for r in recs_sorted
            if str(r.get("CurFYEn", "")) == cur_fy_en and str(r.get("DiscDate", "")) < disc_date_t
        ]
        if not prior_candidates:
            counts.excluded_no_prior_disclosure += 1
            continue
        prior = max(prior_candidates, key=lambda r: str(r.get("DiscDate", "")))
        fop_prev = _num(prior, "FOP")
        fop_prev_field = "FOP"
        if fop_prev is None:
            fop_prev = _num(prior, "FNCOP")
            fop_prev_field = "FNCOP"
        if fop_prev is None or fop_prev <= 0:
            counts.excluded_fop_prev_non_positive += 1
            continue

        progress_ratio = op_value / fop_prev
        raw_sue = progress_ratio - quarter / 4.0

        fop_t = _num(primary, "FOP")
        ta_t = _num(primary, "TA")
        ta_field = "TA"
        if ta_t is None:
            ta_t = _num(primary, "NCTA")
            ta_field = "NCTA" if ta_t is not None else None
            if ta_t is not None:
                counts.used_ncta_fallback += 1

        events.append(
            PeadEvent(
                code=code,
                disc_no=str(primary.get("DiscNo", "")),
                disc_date=disc_date_t,
                disc_time=primary.get("DiscTime"),
                cur_fy_en=cur_fy_en,
                cur_fy_st=str(primary.get("CurFYSt", "")),
                cur_per_en=str(primary.get("CurPerEn", "")),
                cur_per_type_raw=cur_per_type_raw,
                quarter=quarter,
                op_used_field=op_field,
                op_value=op_value,
                fop_prev=fop_prev,
                fop_prev_disc_date=str(prior.get("DiscDate", "")),
                fop_prev_used_field=fop_prev_field,
                progress_ratio=progress_ratio,
                raw_sue=raw_sue,
                fop_t=fop_t,
                ta_t=ta_t,
                ta_used_field=ta_field,
            )
        )
        counts.n_events_kept += 1

    return events


def winsorize_freeze(values: list[float], *, low_pct: float = 1.0, high_pct: float = 99.0):
    """選定期間の分布から1%/99%点を凍結する（spec §3.1手順8）。numpyのみ使用。"""
    import numpy as np

    arr = np.array([v for v in values if v is not None], dtype=float)
    if arr.size == 0:
        return None, None
    low = float(np.percentile(arr, low_pct))
    high = float(np.percentile(arr, high_pct))
    return low, high


def apply_winsorize(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
