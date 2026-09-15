#!/usr/bin/env python3
"""EXP-OBS000006 prescreen 実測: ギャップのプール/イベント件数（前方リターンを一切参照しない）。

適用する除外: E-2B(暦ベース) / E-3 / E-4 / E-5 / E-6 / C-1〜C-3
未適用（prescreen の限界 L-3 として宣言済み）: E-1 / E-2A（銘柄別・厳密な権利落ち/配当落ち）
"""
from __future__ import annotations
import json, math, collections
from pathlib import Path

ROOT = Path('/home/user/SmartWaveRider')
RAW = ROOT/'data'/'raw'/'pead'
OUT = ROOT / 'research' / '_snapshots' / 'prescreen_10y_split'
s1 = json.loads((OUT/'stage1.json').read_text())
N, m = s1['N'], s1['m']
univ = {'selection': s1['universe_selection']['codes'],
        'confirmation': s1['universe_confirmation']['codes']}

codes_all = json.loads((ROOT/'research/EXP-OBS000001/10-result/candidate_codes.json').read_text())['codes']
need = set(univ['selection']) | set(univ['confirmation'])

bars = {}
alldates = set()
for c in codes_all:
    p = RAW/'bars_daily'/f'{c}.json'
    if not p.exists():
        continue
    rows = json.loads(p.read_text())
    alldates.update(r['Date'] for r in rows)
    if c in need:
        bars[c] = {r['Date']: r for r in rows}
T = sorted(alldates)
assert len(T) == N
gi = {d: i+1 for i, d in enumerate(T)}      # date -> 1-based
def T1(k): return T[k-1]

# ---- E-2B: 各暦月の最終営業日の1営業日前 -------------------------------
last_of_month = {}
for i, d in enumerate(T, start=1):
    last_of_month[d[:7]] = i
E2B = set()
for ym, i in last_of_month.items():
    if i-1 >= 1:
        E2B.add(i-1)
print(f'E-2B 一括除外日: {len(E2B)}日')

# ---- 決算開示日（E-3 用。/fins/summary の全 DocType） ------------------
disc = collections.defaultdict(set)
with open(ROOT/'data/raw/pead/fins_summary_all.jsonl') as f:
    for line in f:
        r = json.loads(line)
        c = r.get('Code')
        if c in need and r.get('DiscDate'):
            disc[c].add(r['DiscDate'])
disc_idx = {c: {gi[d] for d in ds if d in gi} for c, ds in disc.items()}

# ---- z の算出 ----------------------------------------------------------
LOOKBACK, WIN = 66, 60

def zseries(code):
    """global index -> (z, g) （E-4/E-5/E-6/E-2B/C-2/C-3 適用後のみ返す）"""
    bd = bars[code]
    own = sorted((gi[d], d) for d in bd if d in gi)
    gap = {}                       # gidx -> g
    valid = {}                     # gidx -> bool (σ窓に使える有効ギャップか)
    for i, d in own:
        if i-1 < 1:
            continue
        pd_ = T1(i-1)
        if pd_ not in bd:
            continue
        o, cp = bd[d].get('O'), bd[pd_].get('C')
        if o is None or cp is None or o <= 0 or cp <= 0:
            continue
        g = math.log(float(o)/float(cp))
        gap[i] = g
        valid[i] = (i not in E2B)          # E-1/E-2A は prescreen では未適用（L-3）
    out = {}
    di = disc_idx.get(code, set())
    for i, d in own:
        if i not in gap:
            continue
        if i in E2B:                       # E-2B: イベント候補側の除外
            continue
        g = gap[i]
        if g >= 0:                         # E-6
            continue
        pd_ = T1(i-1)
        pr = bd[pd_]
        if pr.get('UL') == '1' or pr.get('LL') == '1':     # E-4
            continue
        row = bd[d]
        if row.get('LL') == '1' and row.get('O') is not None and row.get('L') is not None \
           and float(row['O']) == float(row['L']):          # E-5
            continue
        if any(k in di for k in range(i-2, i+3)):           # E-3
            continue
        ws = [gap[j] for j in range(i-LOOKBACK, i) if j in gap and valid.get(j)]
        if len(ws) < WIN:                                   # C-2 相当
            continue
        ws = ws[-WIN:]
        mu = sum(ws)/WIN
        var = sum((x-mu)**2 for x in ws)/(WIN-1)
        if var <= 0:                                        # C-3
            continue
        sd = math.sqrt(var)
        out[i] = (g/sd, g)
    return out

def collect(period, lo, hi):
    pool = collections.defaultdict(list)   # gidx -> [z...]
    for c in univ[period]:
        zs = zseries(c)
        for i, (z, g) in zs.items():
            if lo <= i <= hi and z <= -1.5:
                pool[i].append(z)
    return pool

GAP_SEL = (68, m-11)
GAP_CONF = (m+1, N-11)
pool_sel = collect('selection', *GAP_SEL)
pool_conf = collect('confirmation', *GAP_CONF)

n_sel_eff = sum(1 for i in range(GAP_SEL[0], GAP_SEL[1]+1) if i not in E2B)
n_conf_eff = sum(1 for i in range(GAP_CONF[0], GAP_CONF[1]+1) if i not in E2B)
print(f'n_sel_eff={n_sel_eff}  n_conf_eff={n_conf_eff}')

def pool_stats(pool, label):
    tot = sum(len(v) for v in pool.values())
    days = len(pool)
    k5 = sum(1 for v in pool.values() if len(v) >= 5)
    print(f'[{label}] pool(z<=-1.5): {tot}件 / 相異なる{days}日 / 5件以上の日={k5}')
    return {'pool_total': tot, 'pool_days': days, 'pool_days_ge5': k5}

ps = pool_stats(pool_sel, 'selection')
pc = pool_stats(pool_conf, 'confirmation')

# ---- z* 較正（選定期間のみ・カウントのみ） ------------------------------
GRID = [round(-1.50 - 0.25*k, 2) for k in range(27)]      # -1.50 .. -8.00
assert GRID[-1] == -8.00, GRID[-1]
rows = []
for zs in GRID:
    cnt = sum(sum(1 for z in v if z <= zs) for v in pool_sel.values())
    nann = cnt*245.0/n_sel_eff
    rows.append((zs, cnt, nann))
print('\nz*  選定件数  N_ann')
for zs, cnt, nann in rows:
    print(f'{zs:6.2f} {cnt:8d} {nann:10.2f}')
in_band = [r for r in rows if 150 <= r[2] <= 300]
best = min(rows, key=lambda r: (abs(r[2]-215), r[0]))
zstar = best[0]
print(f'\nDS-2a: [150,300] に入るグリッド点 = {len(in_band)}個 -> {[r[0] for r in in_band]}')
print(f'z* = {zstar}  (選定 N_ann={best[2]:.2f}, 件数={best[1]})')

# ---- 確認期間へ適用 ----------------------------------------------------
ev_conf = collections.Counter()
for i, v in pool_conf.items():
    c = sum(1 for z in v if z <= zstar)
    if c:
        ev_conf[i] = c
ev_total = sum(ev_conf.values())
K_event = len(ev_conf)
nann_conf = ev_total*245.0/n_conf_eff
mx = max(ev_conf.values()) if ev_conf else 0
mxday = T1(max(ev_conf, key=lambda k: ev_conf[k])) if ev_conf else None
share = mx/ev_total if ev_total else None
print(f'\n確認期間イベント: {ev_total}件 / 相異なる{K_event}日 / N_ann={nann_conf:.2f}')
print(f'最大単日 {mxday}: {mx}件 = {share:.2%}')
print('上位10日:', [(T1(k), v) for k, v in sorted(ev_conf.items(), key=lambda kv: -kv[1])[:10]])

ev_sel_total = sum(sum(1 for z in v if z <= zstar) for v in pool_sel.values())
rate_sel = ev_sel_total/n_sel_eff
rate_conf = ev_total/n_conf_eff
ratio = max(rate_sel, rate_conf)/min(rate_sel, rate_conf)
print(f'\n選定イベント {ev_sel_total}件 rate={rate_sel:.4f}/日  確認 rate={rate_conf:.4f}/日  C7-3 ratio={ratio:.3f}')

nd = [len(v) for v in pool_conf.values() if len(v) >= 5]
print(f'確認プール n_d mean={sum(nd)/len(nd):.2f} (K={len(nd)})')

json.dump({'n_sel_eff': n_sel_eff, 'n_conf_eff': n_conf_eff,
           'pool_selection': ps, 'pool_confirmation': pc,
           'grid': rows, 'ds2a_in_band': [r[0] for r in in_band], 'z_star': zstar,
           'z_star_sel_count': best[1], 'z_star_sel_nann': best[2],
           'conf_events': ev_total, 'conf_K_event': K_event, 'conf_nann': nann_conf,
           'conf_max_day': mxday, 'conf_max_day_count': mx, 'conf_max_day_share': share,
           'conf_top10': [(T1(k), v) for k, v in sorted(ev_conf.items(), key=lambda kv: -kv[1])[:10]],
           'sel_events': ev_sel_total, 'rate_sel': rate_sel, 'rate_conf': rate_conf,
           'c7_3_ratio': ratio, 'conf_pool_nd_mean': sum(nd)/len(nd)},
          open(OUT/'stage3_gap.json','w'), ensure_ascii=False, indent=1)
print('stage3 written')
