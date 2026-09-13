#!/usr/bin/env python3
"""EXP-OBS000001（PEAD）フル実装。`research/EXP-OBS000001/00-spec.md` §2〜§7 の実装。

## 位置づけ・前提

spec §8.1 の R-1d 継続確認（`scripts/check_daily_bars_coverage.py`）が
**欠測月なしで完了していること**を前提とする本体パイプライン。
ユニバース構築（§2.2）・SUE-P/SUE-R算出（§3）・約定モデル（§4）・
日本株固有の模擬条件（§6）・選定/確認分割（§7）・G1/G2の生データ出力（§5）を
一気通貫で実行し、`research/EXP-OBS000001/10-result/` に
`prediction-unit.json` / `pipeline.json` / `params.json` / `cost-model.json` /
`run.log` を書き出す。

**本スクリプトはこの実行環境（Claude Codeリモートコンテナ）では実行しない。**
J-Quants APIキーが無い環境で実行してもエラーになるだけである。
司令塔がローカルWindows環境（`.env.local` に `JQUANTS_API_KEY` を設定済み）で
実行することを前提に書かれている。

## 実行方法

    pip install pandas numpy   # 既に requirements.txt に含まれる
    python scripts/exp_obs000001_pead.py

- 依存: `pandas`, `numpy`（`requirements.txt` 既存。`scipy` 等の新規追加はしていない）
- 実行時間の見込み: 候補銘柄数×2エンドポイント（bars/daily, fins/summary）分の
  リクエストが必要で、5秒間隔・429時10秒待機×最大3回リトライを考慮すると
  数十分〜1時間超かかる可能性がある。`data/cache/jquants_v2/` にキャッシュされる
  ため、2回目以降の再実行は速い。
- 出力: `research/EXP-OBS000001/10-result/{prediction-unit,pipeline,params,cost-model}.json`,
  `research/EXP-OBS000001/10-result/run.log`

## 自己判断で埋めていない箇所（実データ確認待ちで例外停止する）

- `ScaleCat`/`Mrgn` の実際のラベル（`lib/pead_universe.py` が検証）
- `DocType`（決算短信の判定）/`CurPerType`（四半期区分の表記）
  （`lib/pead_events.py` が検証）
- `/equities/bars/daily` の効率的なクエリ方式
  （`lib/jquants_client.detect_bars_daily_strategy` が検証）
- `/equities/master`・`/equities/bars/daily`・`/fins/summary` のページネーション
  （検出したら例外停止。憶測で1ページ目のみ採用しない）

これらのいずれかで停止した場合、spec不備または未確認事項としてS戦略チームへ
差し戻すこと（自己解釈で埋めない。EXP-OBS000037の教訓）。

## §6.4（決算跨ぎ回避）の実装上の注記

spec §6.4 は `/equities/earnings-calendar`（次回決算の**予定**日）を参照すべきと
しているが、同エンドポイントの実フィールド名は未確認である。本実装は
`/fins/summary` の生開示履歴から「実際に発生した次回開示日」を代替値として使う
（バックテストにおいては、これは「その時点で市場に知られていた予定日」の
妥当な近似になりうるが、厳密には spec が指定したエンドポイントではない）。
この代替を用いていることを `params.json` に明記する。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.jquants_client import (  # noqa: E402
    BarsDailyStrategy,
    FetchOutcome,
    JQuantsClient,
    UniverseDefinitionError,
    detect_bars_daily_strategy,
    get_api_key,
    load_env_local,
)
from lib.pead_bars import (  # noqa: E402
    build_market_calendar,
    fetch_bars_range_by_code,
    fetch_bars_snapshot_by_date,
    sort_bars_by_date,
)
from lib.pead_costs import cost_model_json, compute_flat_cost_pct  # noqa: E402
from lib.pead_events import (  # noqa: E402
    EventExclusionCounts,
    UnconfirmedFieldValueError,
    apply_winsorize,
    assert_doctype_recognizable,
    assert_quarter_values_recognizable,
    enumerate_doctypes,
    extract_events_for_code,
    winsorize_freeze,
)
from lib.pead_execution import (  # noqa: E402
    CandidateSignal,
    SettlementBucket,
    SlotAllocationConfig,
    allocate_slots,
    determine_entry,
    discretize_position,
    simulate_trailing_stop_trade,
)
from lib.pead_stats import (  # noqa: E402
    cluster_bootstrap_p3,
    decile_monotonicity,
    permutation_p1_block_cyclic_shift,
    permutation_p2_block_shuffle,
    spearman_ic,
)
from lib.pead_universe import (  # noqa: E402
    Season,
    compute_liquidity_window_stats,
    define_seasons,
    judgment_date_for_season,
    load_master_candidates,
    select_season_universe,
)
from step0_api_probe import sanity_check  # noqa: E402

RESULT_DIR = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"
RUN_LOG_PATH = RESULT_DIR / "run.log"

CAPITAL = 1_000_000.0
SEED = 20260913

SELECTION_START = "2024-06-21"
SELECTION_END = "2025-06-30"
CONFIRMATION_START = "2025-07-01"
CONFIRMATION_END = "2026-06-21"

MIN_EVENTS_FOR_JUDGMENT = 300
MIN_BLOCKS_FOR_JUDGMENT = 60


def classify_disc_time(disc_time: str | None) -> str:
    if not disc_time:
        return "欠損"
    text = str(disc_time).strip()
    try:
        # "HH:MM" / "HH:MM:SS" 形式を想定。それ以外は欠損扱いにする。
        hh, mm = text.split(":")[:2]
        minutes = int(hh) * 60 + int(mm)
    except (ValueError, IndexError):
        return "欠損"
    if minutes < 9 * 60:
        return "場前"
    if minutes <= 15 * 60 + 30:
        return "場中"
    return "場後"


def bar_days_after(bars_sorted: list[dict], disc_date: str, n: int) -> list[dict]:
    after = [b for b in bars_sorted if str(b.get("Date", "")) > disc_date]
    return after[:n]


def main() -> int:  # noqa: C901  複雑だが単一パイプラインのオーケストレーションのため許容
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    log.append("# EXP-OBS000001 フル実行ログ\n")
    log.append(f"- 実行日時: {dt.datetime.now().isoformat(timespec='seconds')}")
    log.append("- 再現用実行コマンド: `python scripts/exp_obs000001_pead.py`")
    log.append(f"- 乱数seed（P2/P3のみ）: {SEED}")
    log.append("- 本ログに認証情報は一切含まれない\n")

    def fail(msg: str) -> int:
        log.append(f"\n**停止**: {msg}")
        RUN_LOG_PATH.write_text("\n".join(log), encoding="utf-8")
        print("\n".join(log))
        return 1

    env = load_env_local()
    if not env:
        return fail("`.env.local` が見つからないか空。`JQUANTS_API_KEY` を設定すること。")
    api_key = get_api_key(env, log)
    if not api_key:
        return fail("APIキーを取得できなかった。")

    client = JQuantsClient(api_key, use_cache=not args.no_cache, log_fn=log.append)

    # --- 0. 契約範囲・クエリ方式の実測 -------------------------------------------------
    print("[0] APIキー有効性・契約範囲を確認中...", flush=True)
    ok, sub_start, sub_end, valid_date, valid_code = sanity_check(api_key, log)
    if not ok or not valid_date:
        return fail("APIキーの有効性チェックに失敗した。")
    contract_start = sub_start or dt.date(2024, 6, 21)
    contract_end = sub_end or dt.date(2026, 6, 21)

    strategy = detect_bars_daily_strategy(
        client,
        probe_code=valid_code,
        probe_date=valid_date,
        range_from=contract_start.isoformat(),
        range_to=(contract_start + dt.timedelta(days=60)).isoformat(),
        log=log,
    )
    if strategy == BarsDailyStrategy.PAIR_ONLY:
        return fail(
            "`/equities/bars/daily` の効率的な取得方式が確認できなかった。"
            "全件走査は非現実的なため停止する（詳細はlogを参照）。"
        )

    # --- 1. 候補銘柄（構造条件のみ） --------------------------------------------------
    print("[1] 候補銘柄を抽出中...", flush=True)
    try:
        candidates, master_diag = load_master_candidates(client, log)
    except (RuntimeError, UniverseDefinitionError) as e:
        return fail(str(e))
    candidate_codes = sorted({str(r.get("Code", "")) for r in candidates if r.get("Code")})
    sector_by_code = {str(r.get("Code", "")): str(r.get("S33", "")) for r in candidates}
    log.append(f"候補銘柄数（構造条件のみ）: {len(candidate_codes)}\n")

    # --- 2. 日足バー取得 --------------------------------------------------------------
    print(f"[2] 日足バーを取得中（戦略: {strategy.value}）...", flush=True)
    if strategy == BarsDailyStrategy.RANGE_BY_CODE:
        bars_by_code = fetch_bars_range_by_code(
            client, candidate_codes, contract_start.isoformat(), contract_end.isoformat(), log
        )
    else:
        bars_by_code = fetch_bars_snapshot_by_date(client, set(candidate_codes), contract_start, contract_end, log)
    bars_by_code = sort_bars_by_date(bars_by_code)
    trading_calendar = build_market_calendar(bars_by_code)
    log.append(f"観測された市場カレンダー営業日数: {len(trading_calendar)}\n")

    # --- 3. /fins/summary 取得・イベント抽出 -------------------------------------------
    print("[3] /fins/summary を取得中...", flush=True)
    fins_by_code: dict[str, list[dict]] = {}
    for i, code in enumerate(candidate_codes, 1):
        print(f"  [fins/summary] {i}/{len(candidate_codes)}: code={code}", flush=True)
        result = client.get("/fins/summary", {"code": code})
        if "pagination_key" in (result.body or {}) or "paginationKey" in (result.body or {}):
            return fail(
                f"code={code} の /fins/summary 応答にページネーションキーが含まれる。"
                "ページング方式が未確認のため停止する。"
            )
        recs: list[dict] = []
        if result.body:
            for v in result.body.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    recs = v
                    break
        fins_by_code[code] = recs
        if result.outcome == FetchOutcome.RATE_LIMITED:
            log.append(f"- code={code}: /fins/summary RATE_LIMITED（データ欠損ではない）")

    all_fins_records = [r for recs in fins_by_code.values() for r in recs]
    unique_doctypes = enumerate_doctypes(all_fins_records)
    unique_quarter_values = sorted({str(r.get("CurPerType", "")) for r in all_fins_records if r.get("CurPerType") is not None})
    try:
        assert_doctype_recognizable(unique_doctypes)
        assert_quarter_values_recognizable(unique_quarter_values)
    except UnconfirmedFieldValueError as e:
        return fail(str(e))
    log.append(f"`DocType` ユニーク値: {unique_doctypes}")
    log.append(f"`CurPerType` ユニーク値: {unique_quarter_values}\n")

    counts = EventExclusionCounts()
    events_raw = []
    for code in candidate_codes:
        events_raw.extend(extract_events_for_code(code, fins_by_code.get(code, []), counts))
    log.append(f"イベント抽出診断: {counts}\n")
    if not events_raw:
        return fail("イベントが1件も抽出できなかった。")

    # --- 4. 決算シーズン定義・シーズン単位ユニバース確定（§2.2・§3.2） -----------------
    print("[4] 決算シーズンとシーズン単位ユニバースを確定中...", flush=True)
    unique_disc_dates = sorted({e.disc_date for e in events_raw})
    seasons: list[Season] = define_seasons(unique_disc_dates, trading_calendar)
    season_of_date = {d: s.season_no for s in seasons for d in s.disc_dates}
    log.append(f"決算シーズン数: {len(seasons)}")
    for s in seasons:
        log.append(f"  - シーズン{s.season_no}: {s.start_date}〜{s.end_date} イベント日数={s.n_events}")
    log.append("")

    season_universe: dict[int, list[str]] = {}
    season_universe_detail: dict[int, list[dict]] = {}
    for s in seasons:
        jdate = judgment_date_for_season(s, trading_calendar)
        if jdate is None:
            log.append(f"シーズン{s.season_no}: 判定基準日が算出できない（データ不足）。ユニバース空。")
            season_universe[s.season_no] = []
            continue
        liq_stats = compute_liquidity_window_stats(bars_by_code, jdate, trading_calendar)
        selected = select_season_universe(candidate_codes, liq_stats)
        season_universe[s.season_no] = [r["code"] for r in selected]
        season_universe_detail[s.season_no] = selected
        log.append(
            f"シーズン{s.season_no}: 判定基準日={jdate} 選定銘柄数={len(selected)}"
            f"（構造条件候補{len(candidate_codes)}中）"
        )
    log.append("")

    # --- 5. イベントをシーズン単位ユニバースで絞り込み・SUE-P/FwdRet算出 --------------
    print("[5] SUE-P・FwdRetを算出中...", flush=True)
    rows = []
    n_excluded_not_in_universe = 0
    n_excluded_bar_insufficient = 0
    disc_time_dist = {"場前": 0, "場中": 0, "場後": 0, "欠損": 0}
    for e in events_raw:
        season_no = season_of_date.get(e.disc_date)
        if season_no is None or e.code not in season_universe.get(season_no, []):
            n_excluded_not_in_universe += 1
            continue
        bars_sorted = bars_by_code.get(e.code, [])
        after = bar_days_after(bars_sorted, e.disc_date, 10)
        disc_time_dist[classify_disc_time(e.disc_time)] += 1
        if len(after) < 10:
            n_excluded_bar_insufficient += 1
            continue
        entry = determine_entry(after)
        row = {
            "code": e.code,
            "disc_date": e.disc_date,
            "disc_time": e.disc_time,
            "season_no": season_no,
            "quarter": e.quarter,
            "raw_sue": e.raw_sue,
            "op_used_field": e.op_used_field,
            "fop_prev_used_field": e.fop_prev_used_field,
            "sue_r": ((e.fop_t - e.fop_prev) / e.ta_t) if (e.fop_t is not None and e.ta_t) else None,
            "entry_outcome": entry.outcome.value,
            "entry_price": entry.entry_price,
            "unfilled_counterfactual_h5_gross": entry.counterfactual_h5_gross_return,
            "bars_after": after,  # 後段でトレードシミュレーションに使う（出力はしない）
        }
        if entry.entry_price is not None:
            o1 = after[0]["O"]
            c5 = after[4]["C"]
            row["fwd_ret_1_gross"] = after[0]["C"] / o1 - 1.0
            row["fwd_ret_3_gross"] = after[2]["C"] / o1 - 1.0
            row["fwd_ret_5_gross"] = c5 / o1 - 1.0
            row["fwd_ret_10_gross"] = after[9]["C"] / o1 - 1.0
        rows.append(row)

    df = pd.DataFrame(rows)
    log.append(f"シーズンユニバース外で除外: {n_excluded_not_in_universe}")
    log.append(f"D+10バー不足で除外（G1/G2両方から除外）: {n_excluded_bar_insufficient}")
    log.append(f"DiscTime分布: {disc_time_dist}")
    log.append(f"解析対象イベント総数: {len(df)}\n")
    if df.empty:
        return fail("シーズン/バー不足フィルタ後にイベントが1件も残らなかった。")

    # --- 6. 選定/確認分割・D+10バー日制約（spec §7.1） -------------------------------
    def d10_bar_date(row) -> str | None:
        after = row["bars_after"]
        return str(after[9]["Date"]) if len(after) >= 10 else None

    df["d10_bar_date"] = df.apply(d10_bar_date, axis=1)
    sel_mask = (df["disc_date"] >= SELECTION_START) & (df["disc_date"] <= SELECTION_END) & (
        df["d10_bar_date"] <= SELECTION_END
    )
    conf_mask = (df["disc_date"] >= CONFIRMATION_START) & (df["disc_date"] <= CONFIRMATION_END) & (
        df["d10_bar_date"] <= CONFIRMATION_END
    )
    n_excluded_window_spillover = int((~sel_mask & ~conf_mask & df["disc_date"].between(SELECTION_START, CONFIRMATION_END)).sum())
    df_sel = df[sel_mask].copy()
    df_conf = df[conf_mask].copy()
    log.append(f"選定期間イベント数: {len(df_sel)} / 確認期間イベント数: {len(df_conf)}")
    log.append(f"保有窓が期間境界を跨いで除外: {n_excluded_window_spillover}\n")

    # --- 7. winsorize凍結（選定期間のみ）・θ凍結・単調性用5閾値凍結 -------------------
    sue_low, sue_high = winsorize_freeze(df_sel["raw_sue"].tolist())
    sue_r_low, sue_r_high = winsorize_freeze(df_sel["sue_r"].dropna().tolist())
    for d in (df_sel, df_conf):
        d["raw_sue_w"] = d["raw_sue"].apply(lambda v: apply_winsorize(v, sue_low, sue_high))
        if sue_r_low is not None:
            d["sue_r_w"] = d["sue_r"].apply(lambda v: apply_winsorize(v, sue_r_low, sue_r_high) if pd.notna(v) else None)

    theta_20 = float(np.percentile(df_sel["raw_sue_w"], 80))
    monotonicity_thresholds = {
        p: float(np.percentile(df_sel["raw_sue_w"], 100 - p)) for p in (10, 20, 30, 40, 50)
    }
    log.append(f"winsorize境界（選定期間凍結）: RawSUE=({sue_low},{sue_high}) SUE-R=({sue_r_low},{sue_r_high})")
    log.append(f"θ（上位20%点・凍結）: {theta_20}")
    log.append(f"単調性チェック用5閾値（凍結）: {monotonicity_thresholds}\n")

    # --- 8. G1（予測単位）統計 --------------------------------------------------------
    print("[6] G1統計量を算出中...", flush=True)
    df_conf_g1 = df_conf.dropna(subset=["fwd_ret_5_gross"]).copy()
    n_events = len(df_conf_g1)
    n_blocks = df_conf_g1["disc_date"].nunique()

    ic_main = spearman_ic(df_conf_g1["raw_sue_w"], df_conf_g1["fwd_ret_5_gross"])
    p1 = permutation_p1_block_cyclic_shift(df_conf_g1, "raw_sue_w", "fwd_ret_5_gross", "disc_date", "code")
    p2 = permutation_p2_block_shuffle(df_conf_g1, "raw_sue_w", "fwd_ret_5_gross", "disc_date", n_permutations=10_000, seed=SEED)
    p3 = cluster_bootstrap_p3(df_conf_g1, "raw_sue_w", "fwd_ret_5_gross", "disc_date", n_bootstrap=10_000, seed=SEED)
    decile = decile_monotonicity(df_conf_g1, "raw_sue_w", "fwd_ret_5_gross")

    top20_conf = df_conf_g1[df_conf_g1["raw_sue_w"] >= theta_20]
    top20_conf_mean = float(top20_conf["fwd_ret_5_gross"].mean()) if len(top20_conf) else float("nan")
    top20_sel = df_sel.dropna(subset=["fwd_ret_5_gross"])
    top20_sel = top20_sel[top20_sel["raw_sue_w"] >= theta_20]
    top20_sel_mean = float(top20_sel["fwd_ret_5_gross"].mean()) if len(top20_sel) else float("nan")

    secondary_h = {}
    for h, col in ((1, "fwd_ret_1_gross"), (3, "fwd_ret_3_gross"), (10, "fwd_ret_10_gross")):
        sub = df_conf.dropna(subset=[col])
        secondary_h[f"h{h}"] = {
            "ic": spearman_ic(sub["raw_sue_w"], sub[col]),
            "n": int(len(sub)),
        }

    disc_time_ic = {}
    for bucket in ("場前", "場中", "場後"):
        sub = df_conf_g1[df_conf_g1["disc_time"].apply(classify_disc_time) == bucket]
        if len(sub) >= 2:
            disc_time_ic[bucket] = {"ic": spearman_ic(sub["raw_sue_w"], sub["fwd_ret_5_gross"]), "n": int(len(sub))}

    # §7.2 補助: 銘柄コード昇順で交互に群A/群B。
    # spec は単一の「175銘柄」を想定しているが、本spec§2.2ではユニバースはシーズン単位で
    # 再選定されるため「唯一の175銘柄」は存在しない。ここでは「いずれかのシーズンで
    # 選定された銘柄の和集合」を昇順に並べて交互配分する（記録専用の副次指標であり、
    # spec §7.2 の趣旨＝決定的な機械的分割、を保った上での実務的な読み替え）。
    ever_selected_codes = sorted({c for codes in season_universe.values() for c in codes})
    group_of_code = {c: ("A" if i % 2 == 0 else "B") for i, c in enumerate(ever_selected_codes)}
    df_conf_g1["group"] = df_conf_g1["code"].map(group_of_code)
    group_ic = {
        g: spearman_ic(sub["raw_sue_w"], sub["fwd_ret_5_gross"])
        for g, sub in df_conf_g1.groupby("group")
    }

    block_sizes = df_conf_g1.groupby("disc_date").size()
    prediction_unit = {
        "note": "生データのみ。判定語・解釈は含まない。合否判定はC品質チームが行う。",
        "main_horizon": "h=5",
        "n_events_nominal": n_events,
        "n_unique_discdate_blocks": int(n_blocks),
        "avg_block_size": float(block_sizes.mean()) if len(block_sizes) else None,
        "max_block_size": int(block_sizes.max()) if len(block_sizes) else None,
        "block_size_distribution": block_sizes.value_counts().sort_index().to_dict(),
        "n_singleton_blocks": int((block_sizes == 1).sum()) if len(block_sizes) else 0,
        "judgment_deferred_insufficient_power": bool(n_events < MIN_EVENTS_FOR_JUDGMENT or n_blocks < MIN_BLOCKS_FOR_JUDGMENT),
        "ic_main_h5": ic_main,
        "p1_block_cyclic_shift": {
            "ic_obs": p1.ic_obs,
            "k_blocks": p1.k_blocks,
            "p_value_one_sided": p1.p_value_one_sided,
            "discarded_pairs_by_shift": p1.discarded_pairs_by_shift,
        },
        "p2_block_shuffle": {
            "ic_obs": p2.ic_obs,
            "n_events_used": p2.n_events_used,
            "n_events_excluded_singleton_blocks": p2.n_events_excluded_singleton_blocks,
            "n_permutations": p2.n_permutations,
            "p_value_one_sided": p2.p_value_one_sided,
            "seed": p2.seed,
        },
        "p3_cluster_bootstrap": {
            "ic_point_estimate": p3.ic_point_estimate,
            "ic_ci_95": [p3.ic_ci_low, p3.ic_ci_high],
            "ci_low_above_zero": p3.ci_low_above_zero,
            "n_bootstrap": p3.n_bootstrap,
            "seed": p3.seed,
        },
        "decile_monotonicity": {
            "decile_means": decile.decile_means,
            "decile_counts": decile.decile_counts,
            "spearman_decile_vs_return": decile.spearman_decile_vs_return,
        },
        "top20pct_condition_b": {
            "theta_frozen": theta_20,
            "confirmation_mean_fwd_ret5_gross": top20_conf_mean,
            "confirmation_n": int(len(top20_conf)),
            "selection_mean_fwd_ret5_gross_kill_check": top20_sel_mean,
            "selection_n": int(len(top20_sel)),
        },
        "secondary_horizons": secondary_h,
        "disc_time_distribution_confirmation": disc_time_dist,
        "disc_time_ic_confirmation_record_only": disc_time_ic,
        "b_arm_code_parity_group_ic_record_only": group_ic,
        "sue_r_arm_record_only": {
            "ic_h5": spearman_ic(df_conf.dropna(subset=["sue_r_w", "fwd_ret_5_gross"])["sue_r_w"], df_conf.dropna(subset=["sue_r_w", "fwd_ret_5_gross"])["fwd_ret_5_gross"])
            if "sue_r_w" in df_conf else None,
        },
    }

    # --- 9. G2（パイプライン統合） -----------------------------------------------------
    print("[7] G2パイプラインを算出中...", flush=True)
    df_g2_all = pd.concat([df_sel, df_conf], ignore_index=True)
    df_g2_all = df_g2_all[df_g2_all["entry_outcome"] == "filled"]
    n_unfilled = len(df[df["entry_outcome"] != "filled"])
    unfilled_rows = df[df["entry_outcome"] != "filled"]
    unfilled_rate = n_unfilled / len(df) if len(df) else float("nan")
    filled_h5_mean = df_g2_all["fwd_ret_5_gross"].dropna().mean() if len(df_g2_all) else float("nan")
    counterfactual_mean = unfilled_rows["unfilled_counterfactual_h5_gross"].dropna().mean() if len(unfilled_rows) else float("nan")

    signal_pool = df_g2_all[df_g2_all["raw_sue_w"] >= theta_20].copy()
    signal_pool["entry_date"] = signal_pool["bars_after"].apply(lambda a: str(a[0]["Date"]))
    signal_pool["sector_s33"] = signal_pool["code"].map(sector_by_code).fillna("")

    # §6.4: 次回開示日の代替値（実際に発生した次回開示。/fins/summaryの全履歴から）
    next_disc_by_code_date: dict[tuple[str, str], str | None] = {}
    for code in candidate_codes:
        dates_sorted = sorted({str(r.get("DiscDate", "")) for r in fins_by_code.get(code, []) if r.get("DiscDate")})
        for i, d in enumerate(dates_sorted):
            next_disc_by_code_date[(code, d)] = dates_sorted[i + 1] if i + 1 < len(dates_sorted) else None

    candidates_for_slots = [
        CandidateSignal(
            code=row["code"],
            disc_date=row["disc_date"],
            entry_date=row["entry_date"],
            raw_sue=row["raw_sue_w"],
            sector_s33=row["sector_s33"],
            exit_date="",  # 仮値。トレードシミュレーション後に決定するため2段階で処理する。
        )
        for _, row in signal_pool.iterrows()
    ]
    # exit_date が未知のままではスロット占有解放を判定できないため、
    # 各シグナルを個別に(トレール未加味の)最短仮決済=D+10として先に見積り、
    # 実約定日は後段のシミュレーション結果で置き換えて再割当てする一段階近似とする。
    signal_pool = signal_pool.reset_index(drop=True)
    provisional_exit = {}
    trade_sim_results = {}
    for idx, row in signal_pool.iterrows():
        forced_exit = next_disc_by_code_date.get((row["code"], row["disc_date"]))
        sim = simulate_trailing_stop_trade(row["entry_price"], row["bars_after"], forced_exit_before_date=forced_exit)
        trade_sim_results[idx] = sim
        provisional_exit[idx] = sim.exit_date or str(row["bars_after"][-1]["Date"])

    candidates_for_slots = [
        CandidateSignal(
            code=row["code"],
            disc_date=row["disc_date"],
            entry_date=row["entry_date"],
            raw_sue=row["raw_sue_w"],
            sector_s33=row["sector_s33"],
            exit_date=provisional_exit[idx],
        )
        for idx, row in signal_pool.iterrows()
    ]
    accepted, rejected = allocate_slots(candidates_for_slots, SlotAllocationConfig())
    accepted_keys = {(c.code, c.disc_date) for c in accepted}

    trades = []
    for idx, row in signal_pool.iterrows():
        if (row["code"], row["disc_date"]) not in accepted_keys:
            continue
        sim = trade_sim_results[idx]
        disc_result = discretize_position(row["entry_price"], capital=CAPITAL)
        holding_calendar_days = (
            dt.date.fromisoformat(sim.exit_date) - dt.date.fromisoformat(str(row["bars_after"][0]["Date"]))
        ).days if sim.exit_date else None
        cost = compute_flat_cost_pct(holding_calendar_days or 10)
        gross_ret = (sim.exit_price / sim.entry_price - 1.0) if sim.exit_price else None
        net_ret = (gross_ret - cost.total_flat_cost_pct) if gross_ret is not None else None
        trades.append(
            {
                "code": row["code"],
                "disc_date": row["disc_date"],
                "period": "selection" if row["disc_date"] <= SELECTION_END else "confirmation",
                "entry_price": sim.entry_price,
                "exit_price": sim.exit_price,
                "exit_date": sim.exit_date,
                "bucket": sim.bucket.value,
                "trail_activated": sim.trail_activated,
                "n_bars_held": sim.n_bars_held,
                "carried_over_extra_days": sim.carried_over_extra_days,
                "gross_return": gross_ret,
                "net_return": net_ret,
                "shares": disc_result.shares,
                "actual_notional": disc_result.actual_notional,
                "target_notional_deviation_rate": disc_result.deviation_rate,
                "effective_f": disc_result.effective_f,
                "holding_calendar_days": holding_calendar_days,
                "sector_s33": row["sector_s33"],
            }
        )
    trades_df = pd.DataFrame(trades)

    def period_metrics(sub: pd.DataFrame) -> dict:
        if sub.empty:
            return {"n_trades": 0}
        return {
            "n_trades": int(len(sub)),
            "mean_net_return": float(sub["net_return"].mean()),
            "cumulative_pnl_capital_ratio": float((sub["net_return"] * sub["actual_notional"]).sum() / CAPITAL),
            "mean_holding_business_days": float(sub["n_bars_held"].mean()),
            "mean_holding_calendar_days": float(sub["holding_calendar_days"].mean()),
        }

    sel_trades = trades_df[trades_df["period"] == "selection"] if not trades_df.empty else trades_df
    conf_trades = trades_df[trades_df["period"] == "confirmation"] if not trades_df.empty else trades_df
    sel_metrics = period_metrics(sel_trades)
    conf_metrics = period_metrics(conf_trades)

    # 決済3バケット（§4.5）
    def bucket_stats(sub: pd.DataFrame) -> dict:
        out = {}
        for b in SettlementBucket:
            bsub = sub[sub["bucket"] == b.value] if not sub.empty else sub
            if len(bsub) == 0:
                out[b.value] = {"n": 0}
                continue
            out[b.value] = {
                "n": int(len(bsub)),
                "mean_net_return": float(bsub["net_return"].mean()),
                "median_net_return": float(bsub["net_return"].median()),
                "std_net_return": float(bsub["net_return"].std()) if len(bsub) > 1 else None,
            }
        return out

    # 最大DD（累積PnLの資本比・単純合算ベース。トレード決済日順）
    dd_series = None
    max_dd = None
    if not trades_df.empty:
        ordered = trades_df.sort_values("exit_date")
        pnl_yen = ordered["net_return"] * ordered["actual_notional"]
        cum = pnl_yen.cumsum() + CAPITAL
        running_max = cum.cummax()
        dd = (running_max - cum) / running_max
        max_dd = float(dd.max())

    # 閾値単調性チェック（凍結5閾値。確認期間のみ）
    monotonicity_check = {}
    for pct, thr in monotonicity_thresholds.items():
        sub_conf = df_conf[df_conf["raw_sue_w"] >= thr].dropna(subset=["fwd_ret_5_gross"])
        # このチェックはコスト控除前提だが5閾値グループ別の実トレードシミュレーションは
        # 計算コストが大きいため、簡易的にコスト定数（10暦日想定フラットコスト）を
        # グロスFwdRet5から一律控除した近似値を使う（トレール等の経路依存効果は含まない）。
        approx_cost = compute_flat_cost_pct(10).total_flat_cost_pct + 0.00075
        monotonicity_check[f"top_{pct}pct"] = {
            "threshold_frozen": thr,
            "n": int(len(sub_conf)),
            "approx_cost_adjusted_mean_fwd_ret5": float((sub_conf["fwd_ret_5_gross"] - approx_cost).mean()) if len(sub_conf) else None,
            "note": "簡易近似（フラットコスト控除のみ。トレール等の経路依存効果は含まない）",
        }

    # 実現P&Lベースの簡易Sharpe（参考記録。§5.5 #10）
    sharpe_info = {"note": "参考記録。合否に使わない。2年観測のSE≈0.71（spec §5.3）。", "se_approx": 0.71}
    if not trades_df.empty:
        daily_pnl = trades_df.groupby("exit_date").apply(lambda s: (s["net_return"] * s["actual_notional"]).sum())
        daily_ret = daily_pnl / CAPITAL
        if daily_ret.std() > 0:
            sharpe_info["annualized_sharpe_point_estimate"] = float(daily_ret.mean() / daily_ret.std() * np.sqrt(245))
        sharpe_info["method"] = "決済日ごとの実現P&L合算を日次リターンとみなした簡易近似（真の日次時価評価ではない）"

    n_slot_competition = len(rejected)
    slot_competition_rate = n_slot_competition / (len(accepted) + len(rejected)) if (len(accepted) + len(rejected)) else float("nan")
    counterfactual_rejected_mean = None
    if rejected:
        rejected_keys = {(c.code, c.disc_date) for c in rejected}
        rej_rows = signal_pool[signal_pool.apply(lambda r: (r["code"], r["disc_date"]) in rejected_keys, axis=1)]
        counterfactual_rejected_mean = float(rej_rows["fwd_ret_5_gross"].dropna().mean()) if len(rej_rows) else None

    concurrent_positions_by_day = {}  # 記録: 日次の同時保有数（§6.3のギャップリスク抑制確認用）
    if accepted:
        acc_df = pd.DataFrame([{"entry_date": c.entry_date, "exit_date": c.exit_date} for c in accepted])
        all_days = sorted(set(acc_df["entry_date"]) | set(acc_df["exit_date"]))
        for d in all_days:
            concurrent_positions_by_day[d] = int(((acc_df["entry_date"] <= d) & (acc_df["exit_date"] > d)).sum())
    max_concurrent = max(concurrent_positions_by_day.values()) if concurrent_positions_by_day else 0

    g2_gate = {
        "a_mean_trade_return_positive": bool(conf_metrics.get("mean_net_return", float("nan")) > 0) if conf_metrics.get("n_trades", 0) else None,
        "b_cumulative_pnl_positive": bool(conf_metrics.get("cumulative_pnl_capital_ratio", float("nan")) > 0) if conf_metrics.get("n_trades", 0) else None,
        "c_max_dd_within_15pct": bool(max_dd is not None and max_dd <= 0.15),
        "d_reproducibility_ge_50pct_of_selection": (
            bool(conf_metrics.get("mean_net_return", 0) >= 0.5 * sel_metrics.get("mean_net_return", float("inf")))
            if sel_metrics.get("n_trades", 0) and conf_metrics.get("n_trades", 0) and sel_metrics.get("mean_net_return", 0) > 0
            else None
        ),
        "trade_count_insufficient_judgment_undetermined": conf_metrics.get("n_trades", 0) < 30,
    }

    pipeline = {
        "note": "生データのみ。判定語・解釈は含まない。合否判定はC品質チームが行う。",
        "theta_frozen_top20pct": theta_20,
        "item1_unfillable_rate": unfilled_rate,
        "item2_unfillable_counterfactual_h5_gross": {
            "counterfactual_mean": counterfactual_mean,
            "filled_actual_mean": filled_h5_mean,
            "difference": (counterfactual_mean - filled_h5_mean) if pd.notna(counterfactual_mean) and pd.notna(filled_h5_mean) else None,
        },
        "item3_slot_competition_rate": slot_competition_rate,
        "item4_slot_competition_counterfactual_mean_fwd_ret5": counterfactual_rejected_mean,
        "item5_discretization": (
            trades_df[["code", "disc_date", "entry_price", "shares", "actual_notional", "target_notional_deviation_rate"]].to_dict("records")
            if not trades_df.empty else []
        ),
        "item6_effective_f_by_trade": trades_df[["code", "disc_date", "effective_f"]].to_dict("records") if not trades_df.empty else [],
        "item7_settlement_buckets": {"selection": bucket_stats(sel_trades), "confirmation": bucket_stats(conf_trades)},
        "item8_carried_over_settlement": {
            "n_trades_with_carryover": int((trades_df["carried_over_extra_days"] > 0).sum()) if not trades_df.empty else 0,
            "max_extra_days": int(trades_df["carried_over_extra_days"].max()) if not trades_df.empty else 0,
        },
        "item9_trade_counts_and_holding": {"selection": sel_metrics, "confirmation": conf_metrics},
        "item10_sharpe_reference_only": sharpe_info,
        "item11_disc_time_distribution": disc_time_dist,
        "item12_daily_bars_coverage_reference": "scripts/check_daily_bars_coverage.py の出力（daily-bars-coverage.json）を参照",
        "max_drawdown": max_dd,
        "g2_gate_conditions": g2_gate,
        "threshold_monotonicity_check": monotonicity_check,
        "concurrent_positions_max": max_concurrent,
        "concurrent_positions_by_day_sample": dict(list(concurrent_positions_by_day.items())[:30]),
        "regime_note": {
            "aug_2024_selloff_period": "selection" if "2024-08" <= SELECTION_END else "confirmation",
        },
    }

    # --- 10. 出力書き出し --------------------------------------------------------------
    print("[8] 出力を書き出し中...", flush=True)

    def _json_default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, (pd.Timestamp,)):
            return str(o)
        return str(o)

    (RESULT_DIR / "prediction-unit.json").write_text(
        json.dumps(prediction_unit, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    (RESULT_DIR / "pipeline.json").write_text(
        json.dumps(pipeline, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )

    params = {
        "capital": CAPITAL,
        "seed": SEED,
        "selection_period": [SELECTION_START, SELECTION_END],
        "confirmation_period": [CONFIRMATION_START, CONFIRMATION_END],
        "contract_range_observed": [contract_start.isoformat(), contract_end.isoformat()],
        "bars_daily_query_strategy": strategy.value,
        "master_diagnostics": master_diag,
        "doctype_unique_values": unique_doctypes,
        "curperiodtype_unique_values": unique_quarter_values,
        "n_structural_candidate_codes": len(candidate_codes),
        "seasons": [
            {"season_no": s.season_no, "start": s.start_date, "end": s.end_date, "n_disc_dates": s.n_events}
            for s in seasons
        ],
        "season_universe_sizes": {k: len(v) for k, v in season_universe.items()},
        "season_universe_detail": season_universe_detail,
        "winsorize_bounds_raw_sue": [sue_low, sue_high],
        "winsorize_bounds_sue_r": [sue_r_low, sue_r_high],
        "theta_top20pct_frozen": theta_20,
        "monotonicity_thresholds_frozen": monotonicity_thresholds,
        "event_exclusion_counts": vars(counts),
        "n_excluded_not_in_season_universe": n_excluded_not_in_universe,
        "n_excluded_bar_insufficient": n_excluded_bar_insufficient,
        "n_excluded_period_window_spillover": n_excluded_window_spillover,
        "japan_specific_conditions_applied": {
            "lot_size_100_discretization": True,
            "price_limit_entry_unfillable_check": True,
            "price_limit_settlement_carryover": True,
            "earnings_straddle_forced_exit": True,
            "earnings_straddle_forced_exit_data_source_note": (
                "spec §6.4 は /equities/earnings-calendar を想定しているが実フィールド未確認のため、"
                "/fins/summary の実際の次回開示日（過去実績）を代替値として使用した。"
            ),
            "margin_interest_day_counted_on_actual_calendar_days": True,
            "short_arm_lending_fee_only_zero_hairikubi": True,
            "sector_concentration_cap_s33": True,
            "survivorship_bias_note": master_diag.get("delisting_field_candidates", []),
        },
        "known_gaps_section_7_3": [
            "テール局面は2024年8月急落局面が実質1件のみ。レジーム別有意性は主張できない",
            "2年観測でのSharpe有意性は原理的に示せない（SE≈0.71）",
            "G3/G4はスリーブ在庫ゼロのため判定不能",
            "信用規制（日々公表・増担保）はデータ取得不可のため模擬していない",
            "企業固有の期内季節性はSUE-P定義上除去できていない",
            "生存バイアス: /equities/masterが取得時点の上場銘柄一覧である可能性",
        ],
    }
    (RESULT_DIR / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    (RESULT_DIR / "cost-model.json").write_text(
        json.dumps(cost_model_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log.append("\n## 完了サマリ\n")
    log.append(f"- G1 確認期間 n={n_events} blocks={n_blocks} IC(h5)={ic_main} p1={p1.p_value_one_sided}")
    log.append(f"- G2 確認期間トレード数={conf_metrics.get('n_trades', 0)} 選定期間トレード数={sel_metrics.get('n_trades', 0)}")
    log.append(f"- API呼び出し統計: {client.summary()}")
    try:
        result_dir_display = RESULT_DIR.relative_to(REPO_ROOT)
    except ValueError:
        result_dir_display = RESULT_DIR
    log.append(f"\n出力: {result_dir_display}/{{prediction-unit,pipeline,params,cost-model}}.json")

    RUN_LOG_PATH.write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    print(f"\n--- 出力を書き出しました: {RESULT_DIR} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
