#!/usr/bin/env python3
"""EXP-OBS000001 G1（予測単位）の統計量。spec §5.1・§5.2 の実装。

API を一切呼ばない純粋関数群。`pandas`/`numpy` のみに依存する
（`scipy` は本PJの `requirements.txt` に無いため使わない。Spearman相関は
順位化してからのPearson相関＝Spearman相関という定義に従い自前で実装する）。

すべて決定的。乱数を使うのは P2・P3 のみで、呼び出し側が
`seed=20260913`（spec §5.2 で固定）を渡すことを前提とする。P1 は完全に決定的
（seed不要）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def spearman_ic(x: pd.Series, y: pd.Series) -> float:
    """Spearman順位相関。NaNは事前に呼び出し側で除去しておくこと。"""
    if len(x) < 2:
        return float("nan")
    rx = x.rank(method="average")
    ry = y.rank(method="average")
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(rx.corr(ry))


@dataclass
class P1Result:
    """spec §5.2 P1: 日付ブロックの巡回シフト。主・G1のp値はこれを使う。"""

    ic_obs: float
    k_blocks: int
    ic_by_shift: dict[int, float] = field(default_factory=dict)
    discarded_pairs_by_shift: dict[int, int] = field(default_factory=dict)
    p_value_one_sided: float = float("nan")


def permutation_p1_block_cyclic_shift(
    events: pd.DataFrame, sue_col: str, fwd_ret_col: str, disc_date_col: str, code_col: str
) -> P1Result:
    """spec §5.2 P1 の実装。完全に決定的（seed不要）。

    1. ユニーク DiscDate を昇順にブロック化
    2. 各ブロック内で銘柄コード昇順に並べる
    3. シフト k=1..K-1 について、ブロックB_jのSUEベクトルに
       ブロックB_(j+k mod K)のFwdRetベクトルを対応づける
       （サイズが異なれば先頭からmin件、余りは捨てて件数を記録）
    4. 各kでプールしたSpearman ICを計算
    5. p = (1 + #{k: IC_k >= IC_obs}) / K
    """
    df = events[[disc_date_col, code_col, sue_col, fwd_ret_col]].dropna().copy()
    df = df.sort_values([disc_date_col, code_col])
    unique_dates = sorted(df[disc_date_col].unique())
    k_blocks = len(unique_dates)
    blocks = [df[df[disc_date_col] == d].sort_values(code_col) for d in unique_dates]

    ic_obs = spearman_ic(df[sue_col], df[fwd_ret_col])

    result = P1Result(ic_obs=ic_obs, k_blocks=k_blocks)
    if k_blocks < 2:
        result.p_value_one_sided = float("nan")
        return result

    for k in range(1, k_blocks):
        sue_parts: list[pd.Series] = []
        ret_parts: list[pd.Series] = []
        discarded = 0
        for j in range(k_blocks):
            b_sue = blocks[j][sue_col].reset_index(drop=True)
            b_ret = blocks[(j + k) % k_blocks][fwd_ret_col].reset_index(drop=True)
            n = min(len(b_sue), len(b_ret))
            discarded += (len(b_sue) - n) + (len(b_ret) - n)
            if n == 0:
                continue
            sue_parts.append(b_sue.iloc[:n])
            ret_parts.append(b_ret.iloc[:n])
        pooled_sue = pd.concat(sue_parts, ignore_index=True) if sue_parts else pd.Series(dtype=float)
        pooled_ret = pd.concat(ret_parts, ignore_index=True) if ret_parts else pd.Series(dtype=float)
        ic_k = spearman_ic(pooled_sue, pooled_ret)
        result.ic_by_shift[k] = ic_k
        result.discarded_pairs_by_shift[k] = discarded

    valid_ics = [v for v in result.ic_by_shift.values() if not np.isnan(v)]
    if not valid_ics or np.isnan(ic_obs):
        result.p_value_one_sided = float("nan")
        return result
    count_ge = sum(1 for v in result.ic_by_shift.values() if not np.isnan(v) and v >= ic_obs)
    result.p_value_one_sided = (1 + count_ge) / k_blocks
    return result


@dataclass
class P2Result:
    ic_obs: float
    n_events_used: int
    n_events_excluded_singleton_blocks: int
    n_permutations: int
    p_value_one_sided: float
    seed: int


def permutation_p2_block_shuffle(
    events: pd.DataFrame,
    sue_col: str,
    fwd_ret_col: str,
    disc_date_col: str,
    *,
    n_permutations: int = 10_000,
    seed: int = 20260913,
) -> P2Result:
    """spec §5.2 P2: 同一DiscDateブロック内シャッフル。ブロックサイズ1は除外して記録。"""
    df = events[[disc_date_col, sue_col, fwd_ret_col]].dropna().copy()
    block_sizes = df.groupby(disc_date_col)[sue_col].transform("size")
    excluded = int((block_sizes == 1).sum())
    df = df[block_sizes > 1].reset_index(drop=True)

    ic_obs = spearman_ic(df[sue_col], df[fwd_ret_col])

    rng = np.random.default_rng(seed)
    block_indices: dict = {
        d: df.index[df[disc_date_col] == d].to_numpy() for d in df[disc_date_col].unique()
    }
    fwd_values = df[fwd_ret_col].to_numpy().copy()
    sue_series = df[sue_col]

    count_ge = 0
    for _ in range(n_permutations):
        shuffled = fwd_values.copy()
        for idxs in block_indices.values():
            if len(idxs) > 1:
                perm = rng.permutation(len(idxs))
                shuffled[idxs] = fwd_values[idxs][perm]
        ic_perm = spearman_ic(sue_series, pd.Series(shuffled, index=df.index))
        if not np.isnan(ic_perm) and not np.isnan(ic_obs) and ic_perm >= ic_obs:
            count_ge += 1

    p_value = (1 + count_ge) / (n_permutations + 1) if not np.isnan(ic_obs) else float("nan")
    return P2Result(
        ic_obs=ic_obs,
        n_events_used=len(df),
        n_events_excluded_singleton_blocks=excluded,
        n_permutations=n_permutations,
        p_value_one_sided=p_value,
        seed=seed,
    )


@dataclass
class P3Result:
    ic_ci_low: float
    ic_ci_high: float
    ic_point_estimate: float
    n_bootstrap: int
    seed: int
    ci_low_above_zero: bool


def cluster_bootstrap_p3(
    events: pd.DataFrame,
    sue_col: str,
    fwd_ret_col: str,
    disc_date_col: str,
    *,
    n_bootstrap: int = 10_000,
    seed: int = 20260913,
) -> P3Result:
    """spec §5.2 P3: DiscDateブロックのクラスターブートストラップ。合否基準ではなく参考記録。"""
    df = events[[disc_date_col, sue_col, fwd_ret_col]].dropna().copy()
    unique_dates = df[disc_date_col].unique()
    ic_point = spearman_ic(df[sue_col], df[fwd_ret_col])

    rng = np.random.default_rng(seed)
    n_blocks = len(unique_dates)
    ics: list[float] = []
    grouped = {d: df[df[disc_date_col] == d] for d in unique_dates}
    for _ in range(n_bootstrap):
        sampled_dates = rng.choice(unique_dates, size=n_blocks, replace=True)
        parts = [grouped[d] for d in sampled_dates]
        resampled = pd.concat(parts, ignore_index=True)
        ic = spearman_ic(resampled[sue_col], resampled[fwd_ret_col])
        if not np.isnan(ic):
            ics.append(ic)

    if not ics:
        return P3Result(float("nan"), float("nan"), ic_point, n_bootstrap, seed, False)
    low, high = np.percentile(ics, [2.5, 97.5])
    return P3Result(
        ic_ci_low=float(low),
        ic_ci_high=float(high),
        ic_point_estimate=ic_point,
        n_bootstrap=n_bootstrap,
        seed=seed,
        ci_low_above_zero=bool(low > 0),
    )


@dataclass
class DecileResult:
    decile_means: dict[int, float]
    decile_counts: dict[int, int]
    spearman_decile_vs_return: float


def decile_monotonicity(events: pd.DataFrame, sue_col: str, fwd_ret_col: str) -> DecileResult:
    """spec §5.1 (c): RawSUE十分位別のFwdRet5平均に対する単調性チェック。

    デシル境界は判定対象期間自身の分布から作る（十分位そのものが「その期間の
    相対順位」の診断であり、選定期間の閾値凍結とは別物であるため）。
    """
    df = events[[sue_col, fwd_ret_col]].dropna().copy()
    df["decile"] = pd.qcut(df[sue_col], 10, labels=False, duplicates="drop") + 1
    grouped = df.groupby("decile")[fwd_ret_col]
    means = grouped.mean().to_dict()
    counts = grouped.size().to_dict()
    decile_series = pd.Series(list(means.keys()))
    mean_series = pd.Series(list(means.values()))
    rho = spearman_ic(decile_series, mean_series)
    return DecileResult(
        decile_means={int(k): float(v) for k, v in means.items()},
        decile_counts={int(k): int(v) for k, v in counts.items()},
        spearman_decile_vs_return=rho,
    )
