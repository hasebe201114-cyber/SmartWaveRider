#!/usr/bin/env python3
"""EXP-OBS000001 ユニバース定義。spec §2.2・§3.2 の実装。

- `/equities/master` の構造条件（`ScaleCat`・`Mrgn`）による候補銘柄抽出
  （`scripts/check_daily_bars_coverage.py` と共有するため、ここに集約する）
- 決算シーズンの定義（spec §3.2: 開示が15営業日以上途切れた箇所で区切る）
- シーズン単位の判定基準日（シーズン最初のDiscDateの20営業日前）における
  価格帯（1,000〜3,000円）・流動性（直近20営業日平均売買代金5億円以上）フィルタと
  上位175銘柄の抽出（spec §2.2）

**実データを見ないと確定できない前提**（`ScaleCat`/`Mrgn`の実際のラベル）は
`UniverseDefinitionError` で検知・停止する。憶測で埋めない
（spec §9.3 / EXP-OBS000037 の教訓）。

このモジュール自体は API を呼ばない（`JQuantsClient` を引数で受け取るだけ）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from lib.jquants_client import FetchOutcome, JQuantsClient, UniverseDefinitionError, debug_body_summary

# spec §2.2 条件1: TOPIX Core30 / Large70 / Mid400 相当。
SCALECAT_KEYWORDS = ("core30", "large70", "mid400", "コア30", "ラージ70", "ミッド400")
# spec §2.2 条件2: 制度信用の対象（貸借銘柄 or 制度信用銘柄）。
MRGN_KEYWORDS = ("制度信用", "貸借", "margin", "loan")

PRICE_BAND_LOW = 1000.0
PRICE_BAND_HIGH = 3000.0
VA_MIN = 5.0e8  # 5億円
LIQUIDITY_WINDOW_DAYS = 20
TOP_N = 175
SEASON_GAP_TRADING_DAYS = 15


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    t = text.lower()
    return any(kw.lower() in t for kw in keywords)


def load_master_candidates(client: JQuantsClient, log: list[str]) -> tuple[list[dict], dict]:
    """`/equities/master` を取得し、ScaleCat・Mrgn の構造条件のみで候補銘柄を抽出する。

    戻り値: (候補銘柄レコードのリスト, 診断情報dict)
    """
    log.append("### 候補銘柄抽出（`/equities/master` の構造条件のみ）\n")
    result = client.get("/equities/master", {})
    if result.outcome != FetchOutcome.OK_DATA:
        raise RuntimeError(
            f"/equities/master の取得に失敗した（outcome={result.outcome.value}, "
            f"status={result.status}, err={result.err[:200]}）。候補銘柄を抽出できない。"
        )
    records: list[dict] = []
    for v in result.body.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            records = v
            break
    if not records:
        raise RuntimeError(
            "/equities/master のレスポンスからレコード配列を特定できなかった。"
            f"構造: {debug_body_summary(result.body, max_keys=10)}"
        )
    if "pagination_key" in result.body or "paginationKey" in result.body:
        raise RuntimeError(
            "`/equities/master` にページネーションらしきキーがある。"
            "ページング方式が未確認のため、1ページ目のみの採用は行わずここで停止する。"
            f"レスポンスキー: {list(result.body.keys())}"
        )

    scale_cat_values = sorted({str(r.get("ScaleCat", "")) for r in records if r.get("ScaleCat")})
    scale_cat_nm_values = sorted({str(r.get("ScaleCatNm", "")) for r in records if r.get("ScaleCatNm")})
    mrgn_values = sorted({str(r.get("Mrgn", "")) for r in records if r.get("Mrgn")})
    mrgn_nm_values = sorted({str(r.get("MrgnNm", "")) for r in records if r.get("MrgnNm")})

    scale_hit = any(
        _matches_any(v, SCALECAT_KEYWORDS) for v in scale_cat_values + scale_cat_nm_values
    )
    mrgn_hit = any(_matches_any(v, MRGN_KEYWORDS) for v in mrgn_values + mrgn_nm_values)

    diagnostics = {
        "master_record_count": len(records),
        "scale_cat_unique_values": scale_cat_values,
        "scale_cat_nm_unique_values": scale_cat_nm_values,
        "mrgn_unique_values": mrgn_values,
        "mrgn_nm_unique_values": mrgn_nm_values,
    }
    log.append(f"- `/equities/master` 取得件数: {len(records)}")
    log.append(f"- `ScaleCat` ユニーク値: {scale_cat_values}")
    log.append(f"- `ScaleCatNm` ユニーク値: {scale_cat_nm_values}")
    log.append(f"- `Mrgn` ユニーク値: {mrgn_values}")
    log.append(f"- `MrgnNm` ユニーク値: {mrgn_nm_values}")

    if not scale_hit:
        raise UniverseDefinitionError(
            "`ScaleCat`/`ScaleCatNm` の実データに TOPIX Core30/Large70/Mid400相当と"
            f"判定できるラベルが見つからなかった。観測値: ScaleCat={scale_cat_values} "
            f"ScaleCatNm={scale_cat_nm_values}。独自判断で割り当てず、S戦略チームへ差し戻すこと。"
        )
    if not mrgn_hit:
        raise UniverseDefinitionError(
            "`Mrgn`/`MrgnNm` の実データに制度信用対象と判定できるラベルが見つからなかった。"
            f"観測値: Mrgn={mrgn_values} MrgnNm={mrgn_nm_values}。"
            "独自判断で割り当てず、S戦略チームへ差し戻すこと。"
        )

    candidates = [
        r
        for r in records
        if _matches_any(f"{r.get('ScaleCat', '')} {r.get('ScaleCatNm', '')}", SCALECAT_KEYWORDS)
        and _matches_any(f"{r.get('Mrgn', '')} {r.get('MrgnNm', '')}", MRGN_KEYWORDS)
    ]
    diagnostics["n_structural_candidates"] = len(candidates)
    log.append(f"- ScaleCat×Mrgn 構造条件を満たす候補銘柄数: {len(candidates)}")

    delisting_fields = {
        k for r in records[:50] for k in r.keys() if "delist" in k.lower() or "廃止" in k
    }
    diagnostics["delisting_field_candidates"] = sorted(delisting_fields)
    if not delisting_fields:
        log.append(
            "- ⚠️ 上場廃止日に相当するフィールドが見当たらない。生存バイアスが残存する可能性を記録する。"
        )
    log.append("")
    return candidates, diagnostics


@dataclass
class Season:
    season_no: int
    start_date: str  # 最初の DiscDate
    end_date: str  # 最後の DiscDate
    disc_dates: list[str] = field(default_factory=list)
    n_events: int = 0


def define_seasons(
    unique_disc_dates: list[str], trading_calendar: list[str], *, gap_trading_days: int = SEASON_GAP_TRADING_DAYS
) -> list[Season]:
    """spec §3.2: DiscDateを昇順に並べ、開示が`gap_trading_days`営業日以上
    途切れた箇所でシーズンを区切る。営業日は実測した市場カレンダーで数える。
    """
    dates = sorted(set(unique_disc_dates))
    if not dates:
        return []
    calendar_index = {d: i for i, d in enumerate(trading_calendar)}

    def trading_gap(d1: str, d2: str) -> int:
        """d1からd2までの営業日ギャップ。カレンダーに無い日付は前後の最寄りで代用しない
        （呼び出し側で trading_calendar が DiscDate を包含する範囲であることを前提とする）。
        """
        if d1 in calendar_index and d2 in calendar_index:
            return calendar_index[d2] - calendar_index[d1]
        # カレンダーに無い場合（休日開示等）は暦日ベースで概算し、判定を保守側に倒す
        # （ギャップを実際より小さく見積もらないよう、営業日ではなく暦日をそのまま使う）
        return (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days

    seasons: list[Season] = []
    current = [dates[0]]
    season_no = 1
    for prev, cur in zip(dates, dates[1:]):
        if trading_gap(prev, cur) >= gap_trading_days:
            seasons.append(
                Season(season_no=season_no, start_date=current[0], end_date=current[-1], disc_dates=current)
            )
            season_no += 1
            current = [cur]
        else:
            current.append(cur)
    seasons.append(Season(season_no=season_no, start_date=current[0], end_date=current[-1], disc_dates=current))
    for s in seasons:
        s.n_events = len(s.disc_dates)
    return seasons


def judgment_date_for_season(season: Season, trading_calendar: list[str]) -> str | None:
    """シーズン最初のDiscDateの20営業日前（判定基準日）を市場カレンダーから求める。"""
    if season.start_date not in trading_calendar:
        # DiscDateがカレンダー上の非営業日（開示は非営業日にも発生しうる）の場合、
        # その直前の営業日を起点にする。
        candidates = [d for d in trading_calendar if d < season.start_date]
        if not candidates:
            return None
        anchor_idx = len(candidates) - 1
    else:
        anchor_idx = trading_calendar.index(season.start_date)
    target_idx = anchor_idx - LIQUIDITY_WINDOW_DAYS
    if target_idx < 0:
        return None
    return trading_calendar[target_idx]


def compute_liquidity_window_stats(
    bars_by_code: dict[str, list[dict]], judgment_date: str, trading_calendar: list[str]
) -> dict[str, dict]:
    """判定基準日から遡る直近20営業日の平均終値・平均売買代金を計算する（spec §2.2 条件3・4）。"""
    if judgment_date not in trading_calendar:
        raise RuntimeError(f"judgment_date={judgment_date} が市場カレンダーに存在しない。")
    end_idx = trading_calendar.index(judgment_date)
    start_idx = max(0, end_idx - LIQUIDITY_WINDOW_DAYS + 1)
    window_days = set(trading_calendar[start_idx : end_idx + 1])

    stats: dict[str, dict] = {}
    for code, recs in bars_by_code.items():
        window_recs = [r for r in recs if str(r.get("Date")) in window_days]
        if len(window_recs) < LIQUIDITY_WINDOW_DAYS:
            continue  # データ不足（上場間もない等）。候補から除外。
        closes = [float(r["C"]) for r in window_recs if r.get("C") is not None]
        vas = [float(r["Va"]) for r in window_recs if r.get("Va") is not None]
        if len(closes) < LIQUIDITY_WINDOW_DAYS or len(vas) < LIQUIDITY_WINDOW_DAYS:
            continue
        stats[code] = {
            "avg_close": sum(closes) / len(closes),
            "avg_va": sum(vas) / len(vas),
            "n_days_used": len(window_recs),
        }
    return stats


def select_season_universe(
    candidate_codes: list[str],
    liquidity_stats: dict[str, dict],
    *,
    price_low: float = PRICE_BAND_LOW,
    price_high: float = PRICE_BAND_HIGH,
    va_min: float = VA_MIN,
    top_n: int = TOP_N,
) -> list[dict]:
    """spec §2.2 条件3・4を適用し、Va降順で上位top_n銘柄を採る（不足時は全銘柄）。"""
    qualified = []
    for code in candidate_codes:
        st = liquidity_stats.get(code)
        if st is None:
            continue
        if not (price_low <= st["avg_close"] <= price_high):
            continue
        if st["avg_va"] < va_min:
            continue
        qualified.append({"code": code, **st})
    qualified.sort(key=lambda r: r["avg_va"], reverse=True)
    return qualified[:top_n]
