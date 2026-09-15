#!/usr/bin/env python3
"""EXP-OBS000005 / EXP-OBS000006 prescreen 実測（件数のみ。前方リターンを一切参照しない）。

事前登録: research/EXP-OBS000005/00-prescreen.md §A / research/EXP-OBS000006/00-prescreen.md §A
（コミット b5080ee で凍結済み）
"""
from __future__ import annotations
import json, math, statistics, collections, datetime as dt
from pathlib import Path

ROOT = Path('/home/user/SmartWaveRider')
RAW = ROOT / 'data' / 'raw' / 'pead'
OUT = ROOT / 'research' / '_snapshots' / 'prescreen_10y_split'

LIQ_MIN = 5e8
BAND_MAIN = (1000.0, 3500.0)
BAND_FALLBACK = (700.0, 8000.0)
UNIV_MAX = 175
UNIV_MIN = 150
VA_MIN_OBS = 30

# ---------------------------------------------------------------- load bars
codes = json.loads((ROOT/'research/EXP-OBS000001/10-result/candidate_codes.json').read_text())['codes']
bars: dict[str, dict[str, dict]] = {}
alldates: set[str] = set()
for c in codes:
    p = RAW/'bars_daily'/f'{c}.json'
    if not p.exists():
        continue
    rows = json.loads(p.read_text())
    d = {}
    for r in rows:
        d[r['Date']] = r
    bars[c] = d
    alldates.update(d.keys())

T = sorted(alldates)           # 0-based list; T1(k) = T[k-1]
N = len(T)
m = N // 2
idx = {d: i+1 for i, d in enumerate(T)}   # date -> 1-based index

def T1(k: int) -> str:
    return T[k-1]

D_sel = T1(61)
D_conf = T1(m+1)
print(f'N={N}  T[1]={T1(1)}  T[N]={T1(N)}  m={m}  T[m]={T1(m)}')
print(f'D_sel=T[61]={D_sel}   D_conf=T[m+1]={D_conf}')
print(f'PEAD  selection events T[61..m-16] = {T1(61)} .. {T1(m-16)}')
print(f'PEAD  confirm   events T[m+1..N-16] = {T1(m+1)} .. {T1(N-16)}')
print(f'GAP   selection events T[68..m-11] = {T1(68)} .. {T1(m-11)}')
print(f'GAP   confirm   events T[m+1..N-11] = {T1(m+1)} .. {T1(N-11)}')

# ---------------------------------------------------------------- universe
def median(xs):
    xs = sorted(xs); n = len(xs)
    return xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2.0

def build_universe(win_lo: int, win_hi: int, D: str, label: str):
    """win_lo..win_hi は1-based添字（ちょうど60本）。D はユニバース確定日。"""
    win = [T1(k) for k in range(win_lo, win_hi+1)]
    assert len(win) == 60, len(win)
    rows = []
    for c, bd in bars.items():
        vas = [float(bd[d]['Va']) for d in win if d in bd and bd[d].get('Va') is not None]
        if len(vas) < VA_MIN_OBS:
            continue
        va_med = median(vas)
        # U-5: 確定日 D 時点の終値（D 以前の最新行）
        cand = [d for d in bd if d <= D]
        if not cand:
            continue
        px = bd[max(cand)].get('C')
        if px is None:
            continue
        rows.append((c, va_med, float(px)))
    def apply(band):
        sel = [r for r in rows if r[1] >= LIQ_MIN and band[0] <= r[2] <= band[1]]
        sel.sort(key=lambda r: (-r[1], r[0]))
        return sel
    sel = apply(BAND_MAIN)
    used_band = 'main'
    if len(sel) < UNIV_MIN:
        sel = apply(BAND_FALLBACK)
        used_band = 'fallback'
    capped = sel[:UNIV_MAX]
    print(f'[{label}] D={D} win={win[0]}..{win[-1]}  U1U2cand={len(rows)}  '
          f'U4&U5pass={len(sel)} (band={used_band})  universe={len(capped)}')
    return [r[0] for r in capped], {'D': D, 'window': [win[0], win[-1]],
                                    'u1u2_candidates': len(rows),
                                    'u4u5_pass': len(sel), 'band': used_band,
                                    'universe_size': len(capped)}

univ_sel, meta_sel = build_universe(1, 60, D_sel, 'selection')
univ_conf, meta_conf = build_universe(m-59, m, D_conf, 'confirmation')

# ---------------------------------------------------------------- DS-4 欠損率
def missing_rate(univ, lo, hi):
    tot = 0; miss = 0
    span = [T1(k) for k in range(lo, hi+1)]
    for c in univ:
        bd = bars[c]
        ds = [d for d in bd if span[0] <= d <= span[-1]]
        if not ds:
            continue
        first, last = min(ds), max(ds)
        sub = [d for d in span if first <= d <= last]
        tot += len(sub)
        miss += sum(1 for d in sub if d not in bd)
    return miss/tot if tot else None, tot, miss

mr_sel = missing_rate(univ_sel, 61, m)
mr_conf = missing_rate(univ_conf, m+1, N)
print(f'DS-4 missing rate: selection={mr_sel[0]:.5%} (n={mr_sel[1]})  '
      f'confirmation={mr_conf[0]:.5%} (n={mr_conf[1]})')

json.dump({'N': N, 'm': m, 'T1': T1(1), 'TN': T1(N), 'Tm': T1(m),
           'D_sel': D_sel, 'D_conf': D_conf,
           'pead_sel_range': [T1(61), T1(m-16)], 'pead_conf_range': [T1(m+1), T1(N-16)],
           'gap_sel_range': [T1(68), T1(m-11)], 'gap_conf_range': [T1(m+1), T1(N-11)],
           'universe_selection': meta_sel | {'codes': univ_sel},
           'universe_confirmation': meta_conf | {'codes': univ_conf},
           'ds4_missing_rate_selection': mr_sel[0],
           'ds4_missing_rate_confirmation': mr_conf[0]},
          open(OUT/'stage1.json', 'w'), ensure_ascii=False, indent=1)
print('stage1 written')
