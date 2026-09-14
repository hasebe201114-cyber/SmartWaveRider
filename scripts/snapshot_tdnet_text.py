"""TDnet適時開示PDFの本文テキスト抽出(D-11・司令塔承認済み)。

背景:
  - `scripts/snapshot_tdnet.py` は開示インデックス(時刻・銘柄コード・会社名・
    表題・PDFリンク)のみを保存する。本文PDFそのものは release.tdnet.info の
    サーバ上にあり、消える(または応答しなくなる)タイミングは未確認。
  - 一方でPDFを生バイナリのまま保存すると容量が大きすぎる(実測: 1件あたり
    平均約220KB。全銘柄・全開示種類を対象にすると年間で約17GBに達する見込み)。
  - 司令塔判断: 全銘柄・全開示種類は維持するが、保存形式は**テキスト抽出のみ**
    にする(PDFの生バイナリは保存しない)。実測でテキストは1件あたり数百〜
    数千文字程度(pdfminer.sixでの抽出例: 157KBのPDFから709文字)であり、
    容量は生PDF比で概ね1/50〜1/200に収まる。

技術メモ(2026-09-14実測):
  - PDF取得元は `scripts/snapshot_tdnet.py` が保存した各日のJSON内の
    `rows[].pdf_url`(相対パス)。ベースURLは `https://www.release.tdnet.info/inbs/`。
  - テキスト抽出は `pdfminer.six`(`pdfminer.high_level.extract_text`)を使用。
    環境によっては `cryptography`(pyo3)の `_cffi_backend` 欠落でimportが
    panicすることがあり、`cffi` を先にインストールすることで解消する
    (requirements.txt に明記済み)。
  - スキャン画像PDF(OCR未実施)は抽出結果が空文字になる。空文字は
    「抽出失敗」ではなく「抽出できる文字が無かった」として区別して記録する。

出力: `research/_snapshots/tdnet/YYYY-MM-DD.json` の各行に以下を追記(既存の
インデックス情報はそのまま維持し、この行を追加するだけ):
  - `body_text`: 抽出したテキスト(文字列)。抽出できなかった場合は null
  - `body_text_char_count`: 文字数
  - `body_text_extract_status`: "ok" / "empty" (スキャン画像等) / "fetch_error" / "parse_error"
  - `body_text_extract_error`: エラー内容(status が ok/empty 以外の場合)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.release.tdnet.info/inbs"
OUT_DIR = Path(__file__).resolve().parent.parent / "research" / "_snapshots" / "tdnet"
REQUEST_INTERVAL_SEC = 1.5  # PDF本体は開示インデックスより数が多いため短めだが、無制限にはしない


def _fetch_bytes(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "SmartWaveRider-snapshot/1.0"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except urllib.error.URLError as e:
        raise RuntimeError(f"接続失敗: {url}: {e.reason}") from e


def extract_pdf_text(pdf_bytes: bytes) -> str:
    from pdfminer.high_level import extract_text
    import io

    return extract_text(io.BytesIO(pdf_bytes))


def process_day(json_path: Path, force: bool = False, universe: set[str] | None = None, log=print) -> dict:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    counts = {
        "ok": 0, "empty": 0, "fetch_error": 0, "parse_error": 0,
        "skipped_exists": 0, "skipped_no_pdf": 0, "skipped_outside_universe": 0,
    }

    for row in rows:
        pdf_rel = row.get("pdf_url")
        if not pdf_rel:
            counts["skipped_no_pdf"] += 1
            continue
        if universe is not None and row.get("code") not in universe:
            counts["skipped_outside_universe"] += 1
            continue
        if not force and row.get("body_text_extract_status") is not None:
            counts["skipped_exists"] += 1
            continue

        time.sleep(REQUEST_INTERVAL_SEC)
        url = f"{BASE}/{pdf_rel}"
        try:
            status, pdf_bytes = _fetch_bytes(url)
        except RuntimeError as e:
            row["body_text"] = None
            row["body_text_char_count"] = 0
            row["body_text_extract_status"] = "fetch_error"
            row["body_text_extract_error"] = str(e)
            counts["fetch_error"] += 1
            continue

        if status != 200 or not pdf_bytes:
            row["body_text"] = None
            row["body_text_char_count"] = 0
            row["body_text_extract_status"] = "fetch_error"
            row["body_text_extract_error"] = f"HTTP {status}"
            counts["fetch_error"] += 1
            continue

        try:
            text = extract_pdf_text(pdf_bytes)
        except Exception as e:  # noqa: BLE001 — PDF解析は多様な例外を投げるため広く捕捉し記録する
            row["body_text"] = None
            row["body_text_char_count"] = 0
            row["body_text_extract_status"] = "parse_error"
            row["body_text_extract_error"] = f"{type(e).__name__}: {e}"
            counts["parse_error"] += 1
            continue

        text = text.strip()
        if text:
            row["body_text"] = text
            row["body_text_char_count"] = len(text)
            row["body_text_extract_status"] = "ok"
            row["body_text_extract_error"] = None
            counts["ok"] += 1
        else:
            row["body_text"] = None
            row["body_text_char_count"] = 0
            row["body_text_extract_status"] = "empty"
            row["body_text_extract_error"] = "抽出できる文字なし(スキャン画像PDF等の可能性)"
            counts["empty"] += 1

    data["body_text_extracted_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    tmp_path = json_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp_path.replace(json_path)
    log(f"  {json_path.name}: {counts}")
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD形式で1日だけ処理(省略時は全日)")
    ap.add_argument("--force", action="store_true", help="既に抽出済みの行も再抽出する")
    ap.add_argument(
        "--universe-file",
        type=str,
        default=None,
        help="このJSONの'codes'配列に含まれる銘柄コードのみ処理する"
        "(司令塔判断2026-09-14: 全銘柄追跡は断念し271銘柄ユニバースに限定)",
    )
    args = ap.parse_args()

    universe: set[str] | None = None
    if args.universe_file:
        u = json.loads(Path(args.universe_file).read_text(encoding="utf-8"))
        universe = set(u["codes"])
        print(f"ユニバース限定: {len(universe)}銘柄")

    if args.date:
        targets = [OUT_DIR / f"{args.date}.json"]
    else:
        targets = sorted(OUT_DIR.glob("*.json"))

    total = {
        "ok": 0, "empty": 0, "fetch_error": 0, "parse_error": 0,
        "skipped_exists": 0, "skipped_no_pdf": 0, "skipped_outside_universe": 0,
    }
    for p in targets:
        if not p.exists():
            print(f"[{p.name}] ファイルが存在しない。スキップ")
            continue
        print(f"[{p.name}] 処理開始")
        counts = process_day(p, force=args.force, universe=universe)
        for k, v in counts.items():
            total[k] += v

    print("---")
    print(f"完了: {json.dumps(total, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
