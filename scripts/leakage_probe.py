#!/usr/bin/env python3
"""先読み汚染プローブ（重大論点 C-8 / ACTIVE 0-22 / STRATEGY-BRIEF §3.5）。

問い: **価格データを一切与えずに**、確認期間（2025-07-01〜2026-06-21）の開示について
「その5営業日後の終値（水準）はいくらか」を LLM に問うたとき、真値の ±2% 以内に
入る回答が10%を超えるか。超えれば、モデルの知識カットオフ（2026年5月）による
**先読み汚染が実在する**ことの証明になる。

**解釈の非対称性（事前に明記し、結果を見てから変えない）**:
  - 陽性（>10%）は漏洩の**証明**になる。
  - 陰性（<=10%）は漏洩の**不在の証明にはならない**。したがって陰性でも
    「LLM判断型のバックテストは可能」とは結論しない。

**事前登録した副次検定（プラセボ対照）**:
  水準当ては「その銘柄のだいたいの株価を知っている」だけでも当たりうる。これを
  分離するため、**同一の回答**を、同一銘柄の**無関係な別営業日**の終値に対しても
  採点する。真値ヒット率 ≈ プラセボヒット率 なら、ヒットは日付固有の記憶ではなく
  粗い水準知識で説明される。副次であり、主判定（10%閾値）は変更しない。

使い方:
  python3 scripts/leakage_probe.py make-items      # 出題（価格を含まない）と真値を別ファイルに生成
  python3 scripts/leakage_probe.py score --answers probe/answers.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import statistics
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "pead"
PEAD_RESULT = REPO_ROOT / "research" / "EXP-OBS000001" / "10-result"
PROBE_DIR = REPO_ROOT / "research" / "_snapshots" / "llm_features" / "probe"

# --- 回す前に固定する設計値（事前登録） ---
N_ITEMS = 200
HORIZON_BD = 5  # 開示日から5営業日後
TOL = 0.02  # ±2%
LEAK_THRESHOLD = 0.10  # 10%超で漏洩確定
CONF_START = dt.date(2025, 7, 1)
CONF_END = dt.date(2026, 6, 21)
PLACEBO_MIN_GAP_BD = 20  # プラセボ日は真値日から20営業日以上離す
SEED = 20260914
TARGET_MODEL = "claude-opus-5"  # C-8 が名指しする「判断経路のモデル」


def load_bars(code: str):
    p = RAW_DIR / "bars_daily" / f"{code}.json"
    if not p.exists():
        return None
    rows = json.loads(p.read_text(encoding="utf-8"))
    rows.sort(key=lambda r: r["Date"])
    return rows


def cmd_make_items(args) -> int:
    rnd = random.Random(SEED)
    candidates = set(json.loads((PEAD_RESULT / "candidate_codes.json").read_text(encoding="utf-8"))["codes"])
    master = {
        r["Code"]: r
        for r in json.loads(
            (RAW_DIR / "master_confirmation_start_2025-07-01.json").read_text(encoding="utf-8")
        )["data"]
    }

    # 確認期間内の決算開示（DocType に FinancialStatements を含むもの）を母集団にする
    pool = []
    with (RAW_DIR / "fins_summary_all.jsonl").open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            code = d.get("Code")
            date_s = d.get("DiscDate")
            if code not in candidates or not date_s:
                continue
            date = dt.date.fromisoformat(date_s)
            if not (CONF_START <= date <= CONF_END):
                continue
            if "FinancialStatements" not in (d.get("DocType") or ""):
                continue
            pool.append((code, date_s, d.get("DocType"), d.get("CurPerType")))

    # 1銘柄あたり最大1件に間引く（同一銘柄の重複出題で見かけの n を膨らませない）
    rnd.shuffle(pool)
    seen = set()
    uniq = []
    for rec in pool:
        if rec[0] in seen:
            continue
        seen.add(rec[0])
        uniq.append(rec)

    items, truth = [], []
    bars_cache: dict[str, list] = {}
    for code, date_s, doctype, pertype in uniq:
        bars = bars_cache.get(code) or load_bars(code)
        if bars is None:
            continue
        bars_cache[code] = bars
        idx = next((i for i, r in enumerate(bars) if r["Date"] >= date_s), None)
        if idx is None or idx + HORIZON_BD >= len(bars):
            continue
        target = bars[idx + HORIZON_BD]
        true_close = float(target["C"])
        if true_close <= 0:
            continue
        # プラセボ日: 同一銘柄・確認期間内・真値日から20営業日以上離れた営業日
        conf_idx = [
            i for i, r in enumerate(bars)
            if CONF_START.isoformat() <= r["Date"] <= CONF_END.isoformat()
            and abs(i - (idx + HORIZON_BD)) >= PLACEBO_MIN_GAP_BD
        ]
        if not conf_idx:
            continue
        pl = bars[rnd.choice(conf_idx)]
        m = master.get(code, {})
        mktcap = float(bars[idx].get("MktCap") or 0)
        items.append(
            {
                "item_id": f"{code}_{date_s}",
                "code": code[:4],  # 4桁の一般的な証券コード表記
                "code_jq": code,
                "company_name": m.get("CoName"),
                "disclosure_date": date_s,
                "doc_type": doctype,
                "period_type": pertype,
                "target_date": target["Date"],
                "question_ja": (
                    f"{date_s} に 証券コード {code[:4]}（{m.get('CoName')}）が決算短信を開示した。"
                    f"この銘柄の {target['Date']}（開示日の{HORIZON_BD}営業日後）の終値は何円か。"
                    "1つの数値のみ答えよ。"
                ),
                "_mktcap_rank_input": mktcap,
            }
        )
        truth.append(
            {
                "item_id": f"{code}_{date_s}",
                "true_close": true_close,
                "placebo_date": pl["Date"],
                "placebo_close": float(pl["C"]),
                "mktcap_at_disclosure": mktcap,
            }
        )

    # 時価総額で層別（上位/下位）し、各層から均等に抽出
    order = sorted(range(len(items)), key=lambda i: -items[i]["_mktcap_rank_input"])
    half = len(order) // 2
    top, bottom = order[:half], order[half:]
    n_each = N_ITEMS // 2
    if len(top) < n_each or len(bottom) < n_each:
        print(f"STOP: 母集団不足 top={len(top)} bottom={len(bottom)} 必要={n_each}")
        return 2
    pick = sorted(rnd.sample(top, n_each) + rnd.sample(bottom, n_each))

    out_items, out_truth = [], []
    for i in pick:
        it = dict(items[i])
        it["stratum"] = "mktcap_top_half" if i in set(top) else "mktcap_bottom_half"
        it.pop("_mktcap_rank_input")
        out_items.append(it)
        t = dict(truth[i])
        t["stratum"] = it["stratum"]
        out_truth.append(t)

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    (PROBE_DIR / "items.json").write_text(
        json.dumps(
            {
                "probe": "C-8 lookahead contamination probe",
                "target_model": TARGET_MODEL,
                "n": len(out_items),
                "horizon_business_days": HORIZON_BD,
                "tolerance": TOL,
                "leak_threshold": LEAK_THRESHOLD,
                "seed": SEED,
                "instruction_ja": (
                    "各 item について、価格データ・チャート・外部参照を一切用いずに、"
                    "target_date の終値（円）を1つの数値で答えること。分からない場合も"
                    "必ず数値を1つ答えること（無回答は不可）。"
                ),
                "items": out_items,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    (PROBE_DIR / "truth.json").write_text(
        json.dumps({"n": len(out_truth), "truth": out_truth}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"出題 {len(out_items)}件を {PROBE_DIR/'items.json'} に書き出した（価格を含まない）。")
    print(f"真値は {PROBE_DIR/'truth.json'} に分離して保存した（回答前に読まないこと）。")
    print(f"  層別: top={sum(1 for x in out_items if x['stratum']=='mktcap_top_half')} "
          f"bottom={sum(1 for x in out_items if x['stratum']=='mktcap_bottom_half')}")
    return 0


def cmd_score(args) -> int:
    items = json.loads((PROBE_DIR / "items.json").read_text(encoding="utf-8"))["items"]
    truth = {t["item_id"]: t for t in json.loads((PROBE_DIR / "truth.json").read_text(encoding="utf-8"))["truth"]}
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    amap = {a["item_id"]: a["answer_close"] for a in answers["answers"]}

    rows = []
    for it in items:
        t = truth[it["item_id"]]
        g = amap.get(it["item_id"])
        if g is None:
            continue
        rows.append(
            {
                "item_id": it["item_id"],
                "stratum": it["stratum"],
                "guess": float(g),
                "true_close": t["true_close"],
                "placebo_close": t["placebo_close"],
                "rel_err": float(g) / t["true_close"] - 1.0,
                "rel_err_placebo": float(g) / t["placebo_close"] - 1.0,
            }
        )

    def summarize(sub, key):
        hits = sum(1 for r in sub if abs(r[key]) <= TOL)
        return hits, len(sub), (hits / len(sub) if sub else 0.0)

    res = {
        "probe": "C-8 lookahead contamination probe",
        "target_model": answers.get("model", TARGET_MODEL),
        "administration": answers.get("administration"),
        "n_answered": len(rows),
        "n_items": len(items),
        "tolerance": TOL,
        "leak_threshold": LEAK_THRESHOLD,
        "primary": {},
        "placebo_control": {},
        "by_stratum": {},
        "error_distribution": {},
    }
    h, n, r = summarize(rows, "rel_err")
    res["primary"] = {"hits_within_2pct": h, "n": n, "hit_rate": r,
                      "verdict": "LEAK_CONFIRMED" if r > LEAK_THRESHOLD else "NOT_CONFIRMED"}
    hp, npl, rp = summarize(rows, "rel_err_placebo")
    res["placebo_control"] = {"hits_within_2pct": hp, "n": npl, "hit_rate": rp}
    for s in ("mktcap_top_half", "mktcap_bottom_half"):
        sub = [x for x in rows if x["stratum"] == s]
        h1, n1, r1 = summarize(sub, "rel_err")
        h2, _, r2 = summarize(sub, "rel_err_placebo")
        res["by_stratum"][s] = {"n": n1, "hits": h1, "hit_rate": r1,
                                "placebo_hits": h2, "placebo_hit_rate": r2}
    if rows:
        errs = sorted(abs(x["rel_err"]) for x in rows)
        res["error_distribution"] = {
            "median_abs_rel_err": statistics.median(errs),
            "p10_abs_rel_err": errs[int(len(errs) * 0.10)],
            "p25_abs_rel_err": errs[int(len(errs) * 0.25)],
            "mean_abs_rel_err": statistics.mean(errs),
        }
    res["interpretation_limit"] = (
        "陽性は漏洩の証明になるが、陰性は不在の証明にならない。"
        "本プローブが陰性でも「LLM判断型のバックテストは可能」とは結論しない（C-8）。"
    )
    out = PROBE_DIR / "result.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"saved: {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("make-items")
    sp = sub.add_parser("score")
    sp.add_argument("--answers", required=True)
    args = ap.parse_args()
    return cmd_make_items(args) if args.cmd == "make-items" else cmd_score(args)


if __name__ == "__main__":
    raise SystemExit(main())
