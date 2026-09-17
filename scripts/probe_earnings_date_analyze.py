"""ed_dates.json を解析し、決算発表予定日の「変更」頻度を実測する。"""
import json
import collections
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
d = json.load(open(ROOT / "research" / "_snapshots" / "earnings_date" / "scan_2026-02-02_2026-06-22.json"))
rows_by_date = d["rows"]
errors = d["errors"]

all_rows = []
for day, rows in rows_by_date.items():
    all_rows.extend(rows)

print(f"取得営業日数: {len(rows_by_date)}  エラー日: {len(errors)}")
print(f"総レコード数: {len(all_rows)}")

# グループキー: (Code, FYE, FQName)
g = collections.defaultdict(list)
for r in all_rows:
    g[(r["Code"], r["FYE"], r["FQName"])].append(r)

print(f"ユニークな (Code, FYE, FQName) 企業四半期数: {len(g)}")
empty = [r for r in all_rows if not r.get("SchDate")]
print(f"SchDate が空文字のレコード: {len(empty)}  例: {empty[:3]}")

nrec = collections.Counter(len(v) for v in g.values())
print("1グループあたりのレコード数の分布:", dict(sorted(nrec.items())))

# 変更（SchDate が異なる複数レコード）
changed = {}
for k, v in g.items():
    v = sorted(v, key=lambda r: r["PubDate"])
    sch = [r["SchDate"] for r in v]
    if len(set(sch)) > 1:
        changed[k] = v

print(f"\n予定日が変更されたグループ: {len(changed)} / {len(g)} = {len(changed)/max(len(g),1)*100:.2f}%")

# 変更の方向と幅
def dd(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days

deltas = []
for k, v in changed.items():
    first, last = v[0], v[-1]
    if not (first["SchDate"] and last["SchDate"]):
        continue
    deltas.append((dd(first["SchDate"], last["SchDate"]), k, first["PubDate"], first["SchDate"], last["PubDate"], last["SchDate"]))

deltas.sort()
fwd = [x for x in deltas if x[0] < 0]
bwd = [x for x in deltas if x[0] > 0]
print(f"前倒し(SchDateが早くなった): {len(fwd)}  後ろ倒し: {len(bwd)}")
if deltas:
    vals = [x[0] for x in deltas]
    print(f"変更幅(暦日) min={min(vals)} median={sorted(vals)[len(vals)//2]} max={max(vals)}")
    # 変更告知から新予定日までのリードタイム
    lead = [dd(x[4], x[5]) for x in deltas]
    lead.sort()
    print(f"変更告知(PubDate)→新予定日(SchDate) のリードタイム(暦日): min={lead[0]} p25={lead[len(lead)//4]} median={lead[len(lead)//2]} max={lead[-1]}")
    print("\n変更の例（最大20件）:")
    for x in deltas[:10] + deltas[-10:]:
        print(f"  {x[1]}  {x[2]}:{x[3]} -> {x[4]}:{x[5]}  ({x[0]:+d}日)")

# 175銘柄ユニバースに限定
u = json.load(open(ROOT / "research" / "EXP-OBS000001" / "10-result" / "universe.json"))
codes = set(u["confirmation_universe"]["codes"])
g_u = {k: v for k, v in g.items() if k[0] in codes}
ch_u = {k: v for k, v in changed.items() if k[0] in codes}
print(f"\n[PEAD確認期間ユニバース175銘柄に限定]")
print(f"  企業四半期数: {len(g_u)}  うち変更あり: {len(ch_u)} ({len(ch_u)/max(len(g_u),1)*100:.2f}%)")

# 初回告知のリードタイム分布（level変種の材料）
leads = []
for k, v in g.items():
    v = sorted(v, key=lambda r: r["PubDate"])
    if v[0]["SchDate"]:
        leads.append(dd(v[0]["PubDate"], v[0]["SchDate"]))
leads.sort()
if leads:
    print(f"\n初回告知(PubDate)→予定日(SchDate) リードタイム(暦日): min={leads[0]} p25={leads[len(leads)//4]} median={leads[len(leads)//2]} p75={leads[3*len(leads)//4]} max={leads[-1]}")

# 変更幅の内訳（±2日以内＝事務的 vs 3日以上＝実質的）
minor = [x for x in deltas if abs(x[0]) <= 2]
major = [x for x in deltas if abs(x[0]) >= 3]
print(f"\n変更の内訳: ±2日以内(事務的とみなせる)={len(minor)}  3日以上(実質的)={len(major)}")

# 3月期FY決算の発表予定日の散らばり（"シーズン内の早い/遅い" 変種の材料）
fy0331 = [r for r in all_rows if r["FYE"] == "0331" and r["FQName"] == "FY" and r["SchDate"]]
sch = sorted(set((r["Code"], r["SchDate"]) for r in fy0331))
cnt = collections.Counter(s for _, s in sch)
print(f"\n3月期FY決算: 社数={len(set(c for c,_ in sch))}  予定日のユニーク数={len(cnt)}")
top = cnt.most_common(8)
print("  予定日の集中上位:", top)
if cnt:
    ds = sorted(cnt)
    print(f"  予定日の範囲: {ds[0]} 〜 {ds[-1]}")
