#!/usr/bin/env python3
"""
EXP-OBS000008 prescreen §B meas定用スクリプト。

目的: `data/raw/margin_interest/` に既に永続化済み（0-25完了）の信用取引残高データについて、
件数・銘柄週カバレッジ・ShrtVol=0率・週次グリッドの整合性のみをカウントする。

厳守事項（S戦略チームの事前登録、EXP-OBS000008 00-prescreen.md §A5準拠）:
- 価格・出来高・リターンデータは一切読み込まない・参照しない。
- API呼び出しを一切行わない（既存ローカルJSONの読み取りのみ）。
- 判定語・採否判断は一切出力しない（生カウントのみ）。
- 既存ファイルを一切書き換えない（read-only）。

入力: data/raw/margin_interest/*.json（0-25で取得済み・554銘柄・270,081レコード）
出力: research/EXP-OBS000008/10-result/prescreen_counts.json
"""
import json
import glob
import os
from collections import defaultdict, Counter

RAW_DIR = "data/raw/margin_interest"
OUT_PATH = "research/EXP-OBS000008/10-result/prescreen_counts.json"


def load_all():
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    files = [f for f in files if "state" not in os.path.basename(f)]
    data = {}
    for fp in files:
        code = os.path.basename(fp).replace(".json", "")
        with open(fp) as f:
            recs = json.load(f)
        # 銘柄内で Date 昇順にソート（重複 Date は Date 文字列で安定ソート。重複自体があれば別途カウント）
        recs_sorted = sorted(recs, key=lambda r: r["Date"])
        data[code] = recs_sorted
    return data


def main():
    data = load_all()
    n_codes = len(data)
    total_records = sum(len(v) for v in data.values())

    # --- 1. 週次グリッド W: 全銘柄の Date の和集合（distinct, sorted) ---
    all_dates = set()
    for recs in data.values():
        for r in recs:
            all_dates.add(r["Date"])
    W = sorted(all_dates)
    N_w = len(W)
    m_w = N_w // 2  # floor(N_w/2)

    w_index = {d: i for i, d in enumerate(W)}  # 0-indexed

    # --- 2. 週ごとの銘柄カバレッジ（何銘柄がその週にレコードを持つか） ---
    week_coverage = Counter()
    for recs in data.values():
        seen_weeks_for_code = set()
        for r in recs:
            d = r["Date"]
            if d in seen_weeks_for_code:
                continue  # 同一銘柄・同一Dateの重複は1回のみカウント
            seen_weeks_for_code.add(d)
            week_coverage[d] += 1

    coverage_values = [week_coverage.get(d, 0) for d in W]
    coverage_min = min(coverage_values)
    coverage_max = max(coverage_values)
    coverage_avg = sum(coverage_values) / len(coverage_values)

    # 銘柄数がn_codesの90%未満の週の件数（グリッドの疎密チェック）
    weeks_below_90pct = sum(1 for c in coverage_values if c < 0.9 * n_codes)
    weeks_below_50pct = sum(1 for c in coverage_values if c < 0.5 * n_codes)

    # --- 3. 重複 Date（同一銘柄・同一Dateに複数レコード）の検出 ---
    dup_count = 0
    for code, recs in data.items():
        dates_seen = Counter(r["Date"] for r in recs)
        for d, c in dates_seen.items():
            if c > 1:
                dup_count += 1

    # --- 4. ShrtVol=0 率（M = LongVol/ShrtVol の水準としての算出可否） ---
    total_recs_checked = 0
    shrtvol_zero_count = 0
    for recs in data.values():
        for r in recs:
            total_recs_checked += 1
            if r["ShrtVol"] == 0:
                shrtvol_zero_count += 1
    shrtvol_zero_rate = shrtvol_zero_count / total_recs_checked if total_recs_checked else None

    # --- 5. dM = M_t - M_{t-1}（銘柄自身の直前レコードとの差分）の算出可否 ---
    # 「直前レコード」は当該銘柄の時系列で1つ前（暦週が飛んでいても直前の観測を使う。
    #  週送りの規則性そのものは別途 §6 で確認する）。
    consecutive_pairs = 0
    consecutive_pairs_both_nonzero = 0
    gap_days_counter = Counter()
    for recs in data.values():
        for i in range(1, len(recs)):
            prev, curr = recs[i - 1], recs[i]
            consecutive_pairs += 1
            if prev["ShrtVol"] != 0 and curr["ShrtVol"] != 0:
                consecutive_pairs_both_nonzero += 1
            from datetime import date
            d0 = date.fromisoformat(prev["Date"])
            d1 = date.fromisoformat(curr["Date"])
            gap_days_counter[(d1 - d0).days] += 1

    dm_feasible_rate = (
        consecutive_pairs_both_nonzero / consecutive_pairs if consecutive_pairs else None
    )

    # --- 6. 暦週ギャップの分布（週次グリッドの規則性） ---
    gap_days_sorted = sorted(gap_days_counter.items(), key=lambda x: -x[1])

    # --- 7. 銘柄ごとのレコード数分布 ---
    rec_count_per_code = Counter(len(recs) for recs in data.values())

    # --- 8. 分割点における実日付 ---
    W1 = W[0]
    Wm = W[m_w - 1]  # W[m_w] (1-indexed) -> W[m_w-1] (0-indexed)
    Wm_plus1 = W[m_w]  # W[m_w+1] (1-indexed) -> W[m_w] (0-indexed)
    WN = W[-1]

    # --- 9. 選定/確認 両period（暫定境界、バッファ未適用）の週数・銘柄週数・ShrtVol=0率 ---
    def period_stats(start_idx, end_idx):
        # 0-indexed inclusive range [start_idx, end_idx] over W
        weeks = W[start_idx:end_idx + 1]
        weeks_set = set(weeks)
        n_weeks = len(weeks)
        total_sw = 0  # stock-week観測数（銘柄×週、重複排除後）
        zero_sw = 0
        both_nonzero_pairs = 0
        total_pairs = 0
        coverage_this = []
        for recs in data.values():
            recs_in_period = [r for r in recs if r["Date"] in weeks_set]
            coverage_this.append(len(recs_in_period))
            for j, r in enumerate(recs_in_period):
                total_sw += 1
                if r["ShrtVol"] == 0:
                    zero_sw += 1
        per_week_counts = Counter()
        for recs in data.values():
            for r in recs:
                if r["Date"] in weeks_set:
                    per_week_counts[r["Date"]] += 1
        per_week_values = [per_week_counts.get(w, 0) for w in weeks]
        return {
            "n_weeks": n_weeks,
            "start_date": weeks[0] if weeks else None,
            "end_date": weeks[-1] if weeks else None,
            "total_stock_week_obs": total_sw,
            "shrtvol_zero_stock_week_obs": zero_sw,
            "shrtvol_zero_rate": zero_sw / total_sw if total_sw else None,
            "per_week_stock_count_min": min(per_week_values) if per_week_values else None,
            "per_week_stock_count_max": max(per_week_values) if per_week_values else None,
            "per_week_stock_count_avg": (
                sum(per_week_values) / len(per_week_values) if per_week_values else None
            ),
        }

    # 暫定境界（バッファ未適用。spec側で確定するバッファ後の正式区分とは別）
    selection_provisional = period_stats(0, m_w - 1)
    confirmation_provisional = period_stats(m_w, N_w - 1)

    # 最大単週シェア（暫定・確認期間側）
    conf_weeks = W[m_w:N_w]
    conf_weeks_set = set(conf_weeks)
    per_week_counts_conf = Counter()
    for recs in data.values():
        for r in recs:
            if r["Date"] in conf_weeks_set:
                per_week_counts_conf[r["Date"]] += 1
    conf_total = sum(per_week_counts_conf.values())
    max_week_share = (
        max(per_week_counts_conf.values()) / conf_total if conf_total else None
    )
    max_week_date = (
        max(per_week_counts_conf, key=lambda k: per_week_counts_conf[k])
        if per_week_counts_conf
        else None
    )

    result = {
        "meta": {
            "purpose": "EXP-OBS000008 prescreen §B: counts only, no price/return data referenced, no API calls",
            "input_dir": RAW_DIR,
            "n_codes": n_codes,
            "total_records": total_records,
        },
        "weekly_grid_W": {
            "N_w": N_w,
            "m_w_floor_half": m_w,
            "W_1": W1,
            "W_m": Wm,
            "W_m_plus_1": Wm_plus1,
            "W_N": WN,
        },
        "week_coverage": {
            "coverage_min": coverage_min,
            "coverage_max": coverage_max,
            "coverage_avg": coverage_avg,
            "weeks_below_90pct_of_n_codes": weeks_below_90pct,
            "weeks_below_50pct_of_n_codes": weeks_below_50pct,
        },
        "duplicate_date_per_code_count": dup_count,
        "shrtvol_zero": {
            "total_records_checked": total_recs_checked,
            "shrtvol_zero_count": shrtvol_zero_count,
            "shrtvol_zero_rate": shrtvol_zero_rate,
        },
        "delta_m_feasibility": {
            "consecutive_pairs_total": consecutive_pairs,
            "consecutive_pairs_both_shrtvol_nonzero": consecutive_pairs_both_nonzero,
            "delta_m_feasible_rate": dm_feasible_rate,
        },
        "gap_days_distribution_top15": gap_days_sorted[:15],
        "record_count_per_code_distribution": dict(
            sorted(rec_count_per_code.items(), key=lambda x: -x[1])[:10]
        ),
        "provisional_split_no_buffer": {
            "selection": selection_provisional,
            "confirmation": confirmation_provisional,
            "confirmation_max_single_week_share": max_week_share,
            "confirmation_max_single_week_date": max_week_date,
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
