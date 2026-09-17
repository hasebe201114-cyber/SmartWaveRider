#!/usr/bin/env python3
"""EXP-OBS000005 prescreen 実測: PEAD イベント件数（前方リターンを一切参照しない）。"""
from __future__ import annotations
import json, collections
from pathlib import Path

ROOT = Path('/home/user/SmartWaveRider')
OUT = ROOT / 'research' / '_snapshots' / 'prescreen_10y_split'
s1 = json.loads((OUT/'stage1.json').read_text())

univ_sel = set(s1['universe_selection']['codes'])
univ_conf = set(s1['universe_confirmation']['codes'])
need = univ_sel | univ_conf

PEAD_SEL = tuple(s1['pead_sel_range'])
PEAD_CONF = tuple(s1['pead_conf_range'])

FS_PREFIX = ('1QFinancialStatements', '2QFinancialStatements',
             '3QFinancialStatements', 'FYFinancialStatements')

recs: dict[str, list[dict]] = collections.defaultdict(list)
KEEP = ('DiscDate','DiscTime','DiscNo','Code','DocType','CurPerType','CurFYSt','CurFYEn',
        'NxtFYSt','NxtFYEn','OdP','FOdP','NxFOdP','FSales','NxFSales')
with open(ROOT/'data/raw/pead/fins_summary_all.jsonl') as f:
    for line in f:
        r = json.loads(line)
        c = r.get('Code')
        if c not in need:
            continue
        dtp = r.get('DocType') or ''
        if not (dtp.startswith(FS_PREFIX) or dtp == 'EarnForecastRevision'):
            continue
        recs[c].append({k: r.get(k) for k in KEEP})

def num(v):
    if v is None: return None
    s = str(v).strip()
    if s == '': return None
    try: return float(s)
    except ValueError: return None

def is_fs(r): return (r['DocType'] or '').startswith(FS_PREFIX)
def is_fy_fs(r): return (r['DocType'] or '').startswith('FYFinancialStatements')

def build_events(code: str):
    rs = sorted(recs[code], key=lambda r: (r['DiscDate'] or '', r['DiscTime'] or '', r['DiscNo'] or ''))
    out = []
    for i, r in enumerate(rs):
        if not is_fs(r):
            continue
        if r['CurPerType'] not in ('1Q','2Q','3Q','FY'):
            continue
        # dedup: 同一 DiscDate・同種開示は最後の1件のみ
        dup_later = any(
            (q['DiscDate'] == r['DiscDate'] and is_fs(q) and q['CurPerType'] == r['CurPerType']
             and q['CurFYSt'] == r['CurFYSt'] and q['CurFYEn'] == r['CurFYEn'])
            for q in rs[i+1:] if q['DiscDate'] == r['DiscDate'])
        if dup_later:
            continue
        A = num(r['OdP']) if r['CurPerType'] == 'FY' else num(r['FOdP'])
        if A is None:
            out.append((r['DiscDate'], code, None, 'no_A')); continue
        B = S = None
        for q in reversed(rs[:i]):
            if is_fy_fs(q) and q['NxtFYSt'] == r['CurFYSt'] and q['NxtFYEn'] == r['CurFYEn'] \
               and q['NxtFYSt'] not in (None, ''):
                b, s = num(q['NxFOdP']), num(q['NxFSales'])
            elif q['CurFYSt'] == r['CurFYSt'] and q['CurFYEn'] == r['CurFYEn'] \
                 and q['CurFYSt'] not in (None, ''):
                b, s = num(q['FOdP']), num(q['FSales'])
            else:
                continue
            if b is None:
                continue
            B, S = b, s
            break
        if B is None:
            out.append((r['DiscDate'], code, None, 'no_B')); continue
        if S is None or S <= 0:
            out.append((r['DiscDate'], code, None, 'no_scale')); continue
        out.append((r['DiscDate'], code, (A-B)/S, 'ok'))
    return out

def summarize(univ, lo, hi, label):
    per_day = collections.Counter()
    excl = collections.Counter()
    total = 0
    for c in univ:
        for d, code, sue, st in build_events(c):
            if not (lo <= d <= hi):
                continue
            if st != 'ok':
                excl[st] += 1; continue
            per_day[d] += 1; total += 1
    days = len(per_day)
    k5 = sum(1 for v in per_day.values() if v >= 5)
    mx = max(per_day.values()) if per_day else 0
    mxday = max(per_day, key=lambda d: per_day[d]) if per_day else None
    res = {'label': label, 'range': [lo, hi], 'events': total,
           'distinct_disclosure_days': days,
           'days_with_ge5': k5, 'max_single_day_count': mx,
           'max_single_day_date': mxday,
           'max_single_day_share': (mx/total if total else None),
           'excluded': dict(excl),
           'top10_days': sorted(per_day.items(), key=lambda kv: -kv[1])[:10]}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return res, per_day

r_sel, pd_sel = summarize(univ_sel, *PEAD_SEL, 'selection')
r_conf, pd_conf = summarize(univ_conf, *PEAD_CONF, 'confirmation')

N, m = s1['N'], s1['m']
n_sel_days = (m-16) - 61 + 1
n_conf_days = (N-16) - (m+1) + 1
rate_sel = r_sel['events']/n_sel_days
rate_conf = r_conf['events']/n_conf_days
ratio = max(rate_sel, rate_conf)/min(rate_sel, rate_conf)
print(f'\nn_sel_days={n_sel_days} n_conf_days={n_conf_days}')
print(f'rate_sel={rate_sel:.4f}/日  rate_conf={rate_conf:.4f}/日  C7-3 ratio={ratio:.3f}')

# n_d: 5件以上の日の平均ブロックサイズ
nd = [v for v in pd_conf.values() if v >= 5]
print(f'confirmation: K(=DS-2)={len(nd)}  n_d mean={sum(nd)/len(nd):.2f}  '
      f'events in those days={sum(nd)} ({sum(nd)/r_conf["events"]:.1%} of all)')

json.dump({'selection': r_sel, 'confirmation': r_conf,
           'n_sel_days': n_sel_days, 'n_conf_days': n_conf_days,
           'rate_sel': rate_sel, 'rate_conf': rate_conf, 'c7_3_ratio': ratio,
           'conf_K': len(nd), 'conf_nd_mean': sum(nd)/len(nd)},
          open(OUT/'stage2_pead.json','w'), ensure_ascii=False, indent=1)
print('stage2 written')
