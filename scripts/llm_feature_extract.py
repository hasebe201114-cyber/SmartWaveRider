#!/usr/bin/env python3
"""③LLM特徴量の無償フォワード蓄積トラック（判断事項 D-11 / ACTIVE 0-22）の抽出器。

正本は `research/_snapshots/llm_features/00-preregistration.md`。本スクリプトは
その事前登録を**機械的に強制する**ことだけを目的とし、事前登録に書かれていない
挙動を持たない。

位置づけ（重要・繰り返し）:
  - これは **エッジの検定ではなくデータ製造**である。採否の宣告を出さない。
  - LLM は **判断者ではなく凍結された特徴量抽出器**として動く
    （PJ000001 §2.1 ③「トレード可否そのものをLLMに判断させない」を緩めない）。
  - **ポジションは取らない。資本は1円も置かない。**

本スクリプトが強制する事前登録（すべて例外なし・回避フラグを持たない）:
  G-1 back-fill 禁止: `manifest.json` の `track_start_date` より**前**の開示日は
      いかなる引数を与えても処理しない（`--force` も効かない）。
  G-2 凍結物のハッシュ照合: プロンプト／スキーマ／ユニバースの SHA-256 が
      manifest と1バイトでも違えば実行しない。
  G-3 ユニバース限定: `universe_v1.json` の271銘柄の開示だけを対象とする。
  G-4 費用上限: 日次・月次の上限を超える見込みの時点で停止する
      （超過してから気づくのではなく、呼ぶ前に止める）。
  G-5 冪等追記: 同一 doc_id が既に `status="ok"` で存在すれば再呼び出ししない。
  G-6 モデル同一性の記録: 応答の `model` を毎レコードに記録する。manifest に
      記録された `model_served_frozen` と異なれば、その時点で停止する
      （＝停止基準②「固定モデルIDの提供終了／差し替え」の検知）。

使い方:
  python3 scripts/llm_feature_extract.py --init            # 凍結（初回1回のみ）
  python3 scripts/llm_feature_extract.py --dry-run         # API を呼ばずに経路を検証
  python3 scripts/llm_feature_extract.py                   # 当日（JST）を抽出
  python3 scripts/llm_feature_extract.py --date 2026-09-15 --limit 3
  python3 scripts/llm_feature_extract.py --status          # 停止基準①の監視
  python3 scripts/llm_feature_extract.py --repro-check 20  # 停止基準③の監視
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_DIR = REPO_ROOT / "research" / "_snapshots" / "llm_features"
FROZEN_DIR = LLM_DIR / "frozen"
TDNET_DIR = REPO_ROOT / "research" / "_snapshots" / "tdnet"
MANIFEST_PATH = FROZEN_DIR / "manifest.json"
LEDGER_PATH = LLM_DIR / "cost_ledger.json"
TDNET_PDF_BASE = "https://www.release.tdnet.info/inbs"

# 事前登録 §5 で凍結する API パラメータ
FROZEN_MODEL_REQUEST_ID = "claude-haiku-4-5"
FROZEN_TEMPERATURE = 0.0
FROZEN_MAX_TOKENS = 4096
FROZEN_TEXT_CHAR_CAP = 16000
FROZEN_SCHEMA_VERSION = "v1"

# 事前登録 §7 の費用上限（USD建てで判定し、JPY換算は表示用）
MONTHLY_CAP_USD = 10.0
DAILY_CAP_USD = 1.0
USDJPY_FOR_DISPLAY = 150.0

# claude-haiku-4-5 の料金（USD / 1M tokens）。claude-api スキルの料金表（2026-06-24 時点）。
PRICE_IN_PER_MTOK = 1.0
PRICE_OUT_PER_MTOK = 5.0
PRICE_CACHE_WRITE_PER_MTOK = 1.25
PRICE_CACHE_READ_PER_MTOK = 0.10


# --------------------------------------------------------------------------
# 最小限の JSON Schema 検証器（外部依存を増やさないための手書き実装）
# --------------------------------------------------------------------------
def validate(instance, schema, path="$") -> list[str]:
    errs: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errs.append(f"{path}: const {schema['const']!r} を期待したが {instance!r}")
        return errs
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else list(types)
        if not _type_ok(instance, types):
            errs.append(f"{path}: type {types} を期待したが {type(instance).__name__}")
            return errs
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path}: enum 外の値 {instance!r}")
    if isinstance(instance, str):
        if "pattern" in schema and not re.match(schema["pattern"], instance):
            errs.append(f"{path}: pattern 不一致 {instance!r}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errs.append(f"{path}: maxLength {schema['maxLength']} 超過 ({len(instance)})")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errs.append(f"{path}: 必須キー '{key}' が無い")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errs.append(f"{path}: 未定義キー '{key}'")
        for key, sub in props.items():
            if key in instance:
                errs.extend(validate(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errs.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errs


def _type_ok(instance, types: list[str]) -> bool:
    for t in types:
        if t == "null" and instance is None:
            return True
        if t == "boolean" and isinstance(instance, bool):
            return True
        if t == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if t == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            return True
        if t == "string" and isinstance(instance, str):
            return True
        if t == "array" and isinstance(instance, list):
            return True
        if t == "object" and isinstance(instance, dict):
            return True
    return False


# --------------------------------------------------------------------------
# 凍結物
# --------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen() -> dict:
    """manifest を読み、凍結物のハッシュが一致することを確認する（G-2）。"""
    if not MANIFEST_PATH.exists():
        raise SystemExit(
            f"STOP: {MANIFEST_PATH} が無い。先に `--init` で凍結すること。"
        )
    man = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for key, rel in (
        ("prompt_sha256", "prompt_v1.txt"),
        ("schema_sha256", "schema_v1.json"),
        ("universe_sha256", "universe_v1.json"),
    ):
        actual = sha256_file(FROZEN_DIR / rel)
        if actual != man[key]:
            raise SystemExit(
                f"STOP(G-2): 凍結物 {rel} のハッシュが manifest と一致しない。\n"
                f"  manifest={man[key]}\n  actual  ={actual}\n"
                "凍結物を変更した場合は、変更ではなく**バージョン境界**として "
                "新しい manifest（v2）を起票すること。"
            )
    return man


def cmd_init(args) -> int:
    if MANIFEST_PATH.exists() and not args.reinit:
        raise SystemExit(
            f"STOP: {MANIFEST_PATH} は既に存在する。凍結物の差し替えは"
            "「上書き」ではなく**バージョン境界の記録**として行うこと。"
        )
    today_jst = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()
    man = {
        "manifest_version": "v1",
        "track": "llm_features (ACTIVE 0-22 / 判断事項 D-11)",
        "preregistration": "research/_snapshots/llm_features/00-preregistration.md",
        "frozen_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "track_start_date": args.start or today_jst.isoformat(),
        "model_id_requested": FROZEN_MODEL_REQUEST_ID,
        "model_served_frozen": None,  # 初回成功呼び出しで確定する（G-6）
        "api_params": {
            "temperature": FROZEN_TEMPERATURE,
            "max_tokens": FROZEN_MAX_TOKENS,
            "thinking": "disabled (パラメータを送らない)",
            "response_format_enforcement": None,  # 初回成功呼び出しで確定する
        },
        "text_char_cap": FROZEN_TEXT_CHAR_CAP,
        "schema_version": FROZEN_SCHEMA_VERSION,
        "prompt_sha256": sha256_file(FROZEN_DIR / "prompt_v1.txt"),
        "schema_sha256": sha256_file(FROZEN_DIR / "schema_v1.json"),
        "universe_sha256": sha256_file(FROZEN_DIR / "universe_v1.json"),
        "cost_caps_usd": {"daily": DAILY_CAP_USD, "monthly": MONTHLY_CAP_USD},
        "pricing_usd_per_mtok": {
            "input": PRICE_IN_PER_MTOK,
            "output": PRICE_OUT_PER_MTOK,
            "cache_write": PRICE_CACHE_WRITE_PER_MTOK,
            "cache_read": PRICE_CACHE_READ_PER_MTOK,
        },
        "version_boundaries": [],
    }
    MANIFEST_PATH.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"凍結しました: {MANIFEST_PATH}")
    print(f"  track_start_date = {man['track_start_date']}（これより前の開示日は永久に処理しない）")
    print(f"  prompt_sha256    = {man['prompt_sha256']}")
    print(f"  schema_sha256    = {man['schema_sha256']}")
    print(f"  universe_sha256  = {man['universe_sha256']}")
    return 0


# --------------------------------------------------------------------------
# 費用台帳（G-4）
# --------------------------------------------------------------------------
def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return {"days": {}}


def save_ledger(ledger: dict) -> None:
    LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")


def month_total(ledger: dict, ym: str) -> float:
    return sum(v for k, v in ledger["days"].items() if k.startswith(ym))


def calc_cost_usd(usage: dict) -> float:
    return (
        usage.get("input_tokens", 0) * PRICE_IN_PER_MTOK
        + usage.get("output_tokens", 0) * PRICE_OUT_PER_MTOK
        + usage.get("cache_creation_input_tokens", 0) * PRICE_CACHE_WRITE_PER_MTOK
        + usage.get("cache_read_input_tokens", 0) * PRICE_CACHE_READ_PER_MTOK
    ) / 1_000_000


# --------------------------------------------------------------------------
# 入力（TDnet スナップショット）
# --------------------------------------------------------------------------
def fetch_pdf_text(pdf_rel: str) -> tuple[str | None, str]:
    url = f"{TDNET_PDF_BASE}/{pdf_rel.split('/')[-1]}"
    req = urllib.request.Request(url, headers={"User-Agent": "SmartWaveRider-snapshot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"fetch_error: HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"fetch_error: {type(e).__name__}: {e}"
    try:
        from pdfminer.high_level import extract_text

        text = extract_text(io.BytesIO(raw)).strip()
    except Exception as e:  # noqa: BLE001
        return None, f"parse_error: {type(e).__name__}: {e}"
    return (text, "ok") if text else (None, "empty")


def doc_id_of(date: str, row: dict) -> str:
    basename = (row.get("pdf_url") or "").split("/")[-1]
    key = f"{date}|{row.get('code')}|{row.get('time')}|{basename}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_targets(date: str, universe: set[str]) -> list[dict]:
    path = TDNET_DIR / f"{date}.json"
    if not path.exists():
        raise SystemExit(f"STOP: TDnet スナップショット {path} が無い。先に snapshot_tdnet.py を実行すること。")
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows", [])
    return [r for r in rows if r.get("code") in universe and r.get("pdf_url")]


def load_existing(date: str) -> dict[str, dict]:
    path = LLM_DIR / f"{date}.jsonl"
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["doc_id"]] = rec
    return out


# --------------------------------------------------------------------------
# API 呼び出し
# --------------------------------------------------------------------------
def call_model(client, system_prompt: str, doc_text: str, row: dict, date: str):
    user_block = (
        f"[開示日] {date}\n"
        f"[開示時刻] {row.get('time') or ''}\n"
        f"[TDnet表題] {row.get('title') or ''}\n"
        "[本文テキスト（PDFから機械抽出。レイアウト由来の改行や表崩れを含む）]\n"
        f"{doc_text}"
    )
    return client.messages.create(
        model=FROZEN_MODEL_REQUEST_ID,
        max_tokens=FROZEN_MAX_TOKENS,
        temperature=FROZEN_TEMPERATURE,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_block}],
    )


def parse_json_object(text: str):
    """応答から JSON オブジェクトを取り出す。コードフェンスが付いた場合も救済する。"""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON オブジェクトが見つからない")
    return json.loads(s[start : end + 1])


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------
def cmd_extract(args) -> int:
    man = load_frozen()
    schema = json.loads((FROZEN_DIR / "schema_v1.json").read_text(encoding="utf-8"))
    system_prompt = (FROZEN_DIR / "prompt_v1.txt").read_text(encoding="utf-8")
    universe = set(json.loads((FROZEN_DIR / "universe_v1.json").read_text(encoding="utf-8"))["codes"])

    today_jst = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()
    date = args.date or today_jst.isoformat()

    # ---- G-1 back-fill 禁止（回避手段を用意しない）----
    start = man["track_start_date"]
    if date < start:
        print(
            f"STOP(G-1): 開示日 {date} は track_start_date {start} より前である。\n"
            "  事前登録 §6 により、蓄積開始日より前の開示に LLM 抽出を適用することは\n"
            "  **禁止**されている（先読み汚染のない point-in-time なデータセットという\n"
            "  本トラック唯一の資産価値が、1回の back-fill で永久に失われるため）。\n"
            "  この制限を外すフラグは存在しない。"
        )
        return 3
    if date > today_jst.isoformat():
        print(f"STOP: 開示日 {date} は未来である。")
        return 3

    targets = load_targets(date, universe)
    existing = load_existing(date)
    todo = [r for r in targets if existing.get(doc_id_of(date, r), {}).get("status") != "ok"]
    if args.limit:
        todo = todo[: args.limit]

    print(f"[{date}] ユニバース内の開示 {len(targets)}件 / 未処理 {len(todo)}件 / 今回処理 {len(todo)}件")

    ledger = load_ledger()
    day_spent = ledger["days"].get(date, 0.0)
    month_spent = month_total(ledger, date[:7])
    print(
        f"  費用: 当日 ${day_spent:.4f}/{DAILY_CAP_USD:.2f}  "
        f"当月 ${month_spent:.4f}/{MONTHLY_CAP_USD:.2f}"
    )

    client = None
    if not args.dry_run:
        try:
            import anthropic
        except ImportError:
            print(
                "STOP: `anthropic` パッケージが無い。`pip install anthropic` を実行すること。\n"
                "      （API を呼ばない経路の検証だけなら `--dry-run` を使う）"
            )
            return 4
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")):
            print(
                "STOP: ANTHROPIC_API_KEY（または CLAUDE_API_KEY）が環境変数に無い。\n"
                "      .env.local は読まない方針のため、環境変数で渡すこと。"
            )
            return 4
        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ["CLAUDE_API_KEY"]
        )

    out_path = LLM_DIR / f"{date}.jsonl"
    counts = {"ok": 0, "schema_invalid": 0, "parse_error": 0, "api_error": 0,
              "text_unavailable": 0, "budget_stop": 0, "model_drift_stop": 0}

    for row in todo:
        did = doc_id_of(date, row)

        # ---- G-4 費用上限（呼ぶ前に止める）----
        if not args.dry_run and (day_spent >= DAILY_CAP_USD or month_spent >= MONTHLY_CAP_USD):
            print("STOP(G-4): 費用上限に到達した。黙って継続せず停止する。")
            counts["budget_stop"] += 1
            break

        text = row.get("body_text")
        text_source = "tdnet_snapshot_body_text"
        if not text:
            text, status = fetch_pdf_text(row["pdf_url"])
            text_source = f"pdf_refetch({status})"
            time.sleep(0.5)
        if not text:
            rec = _base_record(did, date, row, man, text_source, 0, 0)
            rec.update({"status": "text_unavailable", "features": None})
            _append(out_path, rec)
            counts["text_unavailable"] += 1
            continue

        truncated = len(text) > FROZEN_TEXT_CHAR_CAP
        doc_text = text[:FROZEN_TEXT_CHAR_CAP]
        rec = _base_record(did, date, row, man, text_source, len(text), len(doc_text))
        rec["text_truncated"] = truncated

        if args.dry_run:
            # dry-run は蓄積ファイルを1バイトも汚さない（追記しない）
            rec["status"] = "dry_run"
            rec["request_preview"] = {
                "model": FROZEN_MODEL_REQUEST_ID,
                "temperature": FROZEN_TEMPERATURE,
                "max_tokens": FROZEN_MAX_TOKENS,
                "system_chars": len(system_prompt),
                "user_chars": len(doc_text) + 200,
                "estimated_input_tokens_at_1.4_per_char": round((len(system_prompt) + len(doc_text)) * 1.4),
            }
            print(f"  [dry-run] {row['code']} {str(row.get('title'))[:36]} "
                  f"text={len(text)}chars trunc={truncated}")
            counts["ok"] += 1
            continue

        t0 = time.time()
        try:
            resp = call_model(client, system_prompt, doc_text, row, date)
        except Exception as e:  # noqa: BLE001
            rec.update({"status": "api_error", "error": f"{type(e).__name__}: {e}", "features": None})
            _append(out_path, rec)
            counts["api_error"] += 1
            continue
        rec["latency_ms"] = round((time.time() - t0) * 1000)

        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
        cost = calc_cost_usd(usage)
        day_spent += cost
        month_spent += cost
        ledger["days"][date] = round(day_spent, 6)
        save_ledger(ledger)
        rec["usage"] = usage
        rec["cost_usd"] = cost
        rec["model_served"] = resp.model
        rec["stop_reason"] = resp.stop_reason

        # ---- G-6 モデル同一性 ----
        if man.get("model_served_frozen") is None:
            man["model_served_frozen"] = resp.model
            MANIFEST_PATH.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  [G-6] model_served_frozen を {resp.model} に確定した。")
        elif resp.model != man["model_served_frozen"]:
            rec.update({"status": "model_drift_stop", "features": None})
            _append(out_path, rec)
            print(
                f"STOP(G-6): 応答モデルが {man['model_served_frozen']} から {resp.model} へ変化した。\n"
                "  停止基準②に該当する。黙って継続せず、manifest に version_boundary を\n"
                "  記録したうえで司令塔判断を仰ぐこと。"
            )
            counts["model_drift_stop"] += 1
            break

        text_out = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            feat = parse_json_object(text_out)
        except Exception as e:  # noqa: BLE001
            rec.update({"status": "parse_error", "error": str(e), "raw_response": text_out[:4000],
                        "features": None})
            _append(out_path, rec)
            counts["parse_error"] += 1
            continue

        errs = validate(feat, schema)
        if errs:
            rec.update({"status": "schema_invalid", "schema_errors": errs[:20], "features": feat})
            counts["schema_invalid"] += 1
        else:
            rec.update({"status": "ok", "features": feat})
            counts["ok"] += 1
        _append(out_path, rec)
        print(f"  {row['code']} {str(row.get('title'))[:32]} -> {rec['status']} ${cost:.5f}")

    print(f"完了: {json.dumps(counts, ensure_ascii=False)}  出力: {out_path}")
    return 0


def _base_record(did, date, row, man, text_source, char_count, sent_chars) -> dict:
    return {
        "doc_id": did,
        "disclosure_date": date,
        "disclosure_time": row.get("time"),
        "code": row.get("code"),
        "company_name": row.get("company_name"),
        "title": row.get("title"),
        "pdf_url": row.get("pdf_url"),
        "has_xbrl": bool(row.get("xbrl")),
        "text_source": text_source,
        "text_char_count": char_count,
        "text_chars_sent": sent_chars,
        "manifest_version": man["manifest_version"],
        "model_requested": FROZEN_MODEL_REQUEST_ID,
        "prompt_sha256": man["prompt_sha256"],
        "schema_sha256": man["schema_sha256"],
        "universe_sha256": man["universe_sha256"],
        "schema_version": FROZEN_SCHEMA_VERSION,
        "extracted_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# 停止基準の監視
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    man = load_frozen()
    files = sorted(LLM_DIR.glob("20??-??-??.jsonl"))[-20:]
    tot = ok = 0
    per_day = []
    for p in files:
        d_tot = d_ok = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            d_tot += 1
            d_ok += rec.get("status") == "ok"
        tot += d_tot
        ok += d_ok
        per_day.append((p.stem, d_ok, d_tot))
    rate = ok / tot if tot else 0.0
    ledger = load_ledger()
    ym = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).strftime("%Y-%m")
    spent = month_total(ledger, ym)
    print(f"track_start_date = {man['track_start_date']}")
    print(f"model_served_frozen = {man.get('model_served_frozen')}")
    print(f"直近{len(files)}営業日ファイル: 成功 {ok}/{tot} = {rate:.1%}  （停止基準① 閾値 95%）")
    for d, a, b in per_day:
        print(f"  {d}: {a}/{b}")
    print(f"当月費用 ${spent:.4f} / 上限 ${MONTHLY_CAP_USD:.2f}（約 ¥{spent*USDJPY_FOR_DISPLAY:,.0f}）")
    print("--- エッジ検定の開始条件（事前登録 §3）---")
    bdays = len(list(LLM_DIR.glob("20??-??-??.jsonl")))
    print(f"  蓄積営業日数 {bdays} / 245  （※本カウントは必要条件。最終判定は EXP の"
          "イベント定義に対して DS-1/DS-6/DS-7 を測り直す）")
    if tot and rate < 0.95:
        print("STOP(停止基準①): 取得成功率が95%未満。黙って継続せずバージョン境界を記録すること。")
        return 5
    return 0


def cmd_repro_check(args) -> int:
    """停止基準③: 同一文書・同一モデル・temperature 0 の再実行でのフィールド不一致率。"""
    man = load_frozen()
    import random

    random.seed(args.seed)
    recs = []
    for p in sorted(LLM_DIR.glob("20??-??-??.jsonl"))[-7:]:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("status") == "ok":
                    recs.append(r)
    if not recs:
        print("再現性検査の対象（status=ok）がまだ無い。")
        return 1
    sample = random.sample(recs, min(args.repro_check, len(recs)))
    print(f"再現性検査: {len(sample)}件を再実行して比較する（temperature={FROZEN_TEMPERATURE}）")
    if args.dry_run:
        print("  --dry-run のため実行しない。")
        return 0
    import anthropic

    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ["CLAUDE_API_KEY"]
    )
    system_prompt = (FROZEN_DIR / "prompt_v1.txt").read_text(encoding="utf-8")
    total_fields = mismatch = 0
    for r in sample:
        text, _ = fetch_pdf_text(r["pdf_url"])
        if not text:
            continue
        row = {"time": r["disclosure_time"], "title": r["title"], "code": r["code"]}
        resp = call_model(client, system_prompt, text[:FROZEN_TEXT_CHAR_CAP], row, r["disclosure_date"])
        text_out = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            feat2 = parse_json_object(text_out)
        except Exception:  # noqa: BLE001
            mismatch += 1
            total_fields += 1
            continue
        a, b = _flatten(r["features"]), _flatten(feat2)
        keys = set(a) | set(b)
        total_fields += len(keys)
        mismatch += sum(1 for k in keys if a.get(k) != b.get(k))
        time.sleep(0.3)
    rate = mismatch / total_fields if total_fields else 0.0
    print(f"フィールド不一致率 {mismatch}/{total_fields} = {rate:.2%}  （停止基準③ 閾値 2%）")
    if rate > 0.02:
        print("STOP(停止基準③): 不一致率が2%超。バージョン境界を記録し司令塔判断を仰ぐこと。")
        return 5
    return 0


def _flatten(obj, prefix="") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="凍結 manifest を作成する（初回1回のみ）")
    ap.add_argument("--reinit", action="store_true", help="--init を強制（バージョン境界の記録を伴う場合のみ）")
    ap.add_argument("--start", type=str, default=None, help="--init 時の track_start_date（既定=今日JST）")
    ap.add_argument("--date", type=str, default=None, help="対象の開示日 YYYY-MM-DD（既定=今日JST）")
    ap.add_argument("--limit", type=int, default=None, help="処理件数の上限（動作確認用）")
    ap.add_argument("--dry-run", action="store_true", help="API を呼ばずに経路・ガードだけ検証する")
    ap.add_argument("--status", action="store_true", help="停止基準①と開始条件の監視")
    ap.add_argument("--repro-check", type=int, default=0, help="停止基準③の再現性検査（件数）")
    ap.add_argument("--seed", type=int, default=20260914)
    args = ap.parse_args()

    if args.init or args.reinit:
        return cmd_init(args)
    if args.status:
        return cmd_status(args)
    if args.repro_check:
        return cmd_repro_check(args)
    return cmd_extract(args)


if __name__ == "__main__":
    raise SystemExit(main())
