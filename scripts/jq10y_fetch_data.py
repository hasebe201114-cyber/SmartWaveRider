#!/usr/bin/env python3
"""EXP-OBS000005 / EXP-OBS000006（10年版）共通データ取得スクリプト。

10Y-COMMON §8 D-0・D-1・D-3・D-4・D-5・D-7 を実装する。
`pead_fetch_data.py` / `gap_fetch_data.py` を統合・拡張したもの。

取得対象・保存先（すべて `data/raw/jq10y/` 配下に永続化。D-18/N-13）:
  - D-0: 契約範囲の実測確定（プローブのみ。価格の中身は見ない）
  - D-1: `/equities/bars/daily?date=YYYY-MM-DD` を全営業日について取得し、
         1日1ファイルとして `bars_by_date/YYYY-MM-DD.json` に保存する。
  - D-3: `/equities/master?date=YYYY-MM-DD` を確定日集合 `U_dates` について取得し
         `master_by_date/YYYY-MM-DD.json` に保存する（U_datesは事前に別途計算して渡す）。
  - D-4: `/fins/summary?date=YYYY-MM-DD` を全営業日について取得し
         `fins_summary_by_date/YYYY-MM-DD.json` に保存する。
  - D-5: `/fins/earnings-date?date=YYYY-MM-DD` を全営業日について取得し
         `earnings_date_by_date/YYYY-MM-DD.json` に保存する。

すべてレジューム可能（stateファイルに完了日を記録し、再実行時はスキップする）。
HTTP 429はクライアント側で指数バックオフ。それでも解消しない日は失敗日として記録し、
真の欠測（0件・祝日等）と混同しない。

使い方:
    python scripts/jq10y_fetch_data.py --step probe
    python scripts/jq10y_fetch_data.py --step bars
    python scripts/jq10y_fetch_data.py --step fins
    python scripts/jq10y_fetch_data.py --step earnings
    python scripts/jq10y_fetch_data.py --step master --dates-file <U_dates.json>

環境変数: JQUANTS_API_KEY（必須。.env.local は読まない）
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.jquants_client import JQuantsClient, RateLimitExhaustedError, business_days, stderr_log  # noqa: E402


class RollingRateLimiter:
    """スレッドセーフな直近60秒スライディングウィンドウ・レート制限（120件/分）。

    APIコールごとの実ネットワーク往復（実測 約3〜4秒/件）がボトルネックになるため、
    直列実行では120件/分の許容枠を使い切れない。少数スレッドで並列化しつつ、
    このリミッタで合計リクエスト数を120件/分以内に抑える。
    """

    def __init__(self, max_per_minute: int = 110):
        self.max_per_minute = max_per_minute
        self._lock = threading.Lock()
        self._timestamps: collections.deque = collections.deque()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > 60.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(now)
                    return
                wait = 60.0 - (now - self._timestamps[0]) + 0.05
            time.sleep(max(wait, 0.05))

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "jq10y"

# 実行日の直近営業日までを走査する（未来日は投げない）。開始日は契約範囲より
# 十分前（2016-01-01）からスキャンし、400応答（契約範囲外）は「圏外」として記録するのみで
# 失敗扱いにしない（D-0の実測そのもの）。
SCAN_START = dt.date(2016, 1, 1)


def today_jst() -> dt.date:
    # コンテナのタイムゾーンに依存しないよう、実行日はUTC+9で厳密計算しない。
    # 日足は「直近営業日まで」で十分なため、実行日そのものを終端とする（未来日はAPIが0件/400を返すだけ）。
    return dt.date.today()


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"done_dates": [], "out_of_range_dates": [], "zero_dates": [], "failed_dates": []}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def step_probe() -> dict:
    """D-0: 契約範囲の実測確定。価格の中身には一切触れない（件数のみ記録）。"""
    client = JQuantsClient(log_fn=stderr_log, min_interval=0.6)
    result: dict = {"probe_endpoint": "/equities/bars/daily"}

    # 最小日付の二分探索的プローブ（400=圏外 / 200=圏内）。2016-09-10〜09-20を実測。
    probe_dates = [dt.date(2016, 9, d) for d in range(10, 21)]
    boundary_scan = []
    for d in probe_dates:
        try:
            body = client.get("/equities/bars/daily", {"date": d.isoformat()})
            boundary_scan.append({"date": d.isoformat(), "status": "in_range", "record_count": len(body.get("data", []))})
        except RuntimeError as e:
            msg = str(e)
            if "400" in msg:
                boundary_scan.append({"date": d.isoformat(), "status": "out_of_range", "detail": msg[:300]})
            else:
                boundary_scan.append({"date": d.isoformat(), "status": "error", "detail": msg[:300]})
    result["min_date_boundary_scan"] = boundary_scan
    in_range = [r["date"] for r in boundary_scan if r["status"] == "in_range"]
    result["measured_min_date"] = min(in_range) if in_range else None

    # 最大日付（直近営業日）の実測: 実行日から遡って最初にrecord_count>0となる日を探す
    end_probe = []
    d = today_jst()
    found_max = None
    for _ in range(10):
        try:
            body = client.get("/equities/bars/daily", {"date": d.isoformat()})
            n = len(body.get("data", []))
            end_probe.append({"date": d.isoformat(), "record_count": n})
            if n > 0 and found_max is None:
                found_max = d.isoformat()
        except RuntimeError as e:
            end_probe.append({"date": d.isoformat(), "error": str(e)[:300]})
        d -= dt.timedelta(days=1)
    result["max_date_probe"] = end_probe
    result["measured_max_date"] = found_max
    result["executed_at"] = dt.datetime.now().isoformat()
    result["executed_date"] = today_jst().isoformat()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / "d0_contract_range_probe.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    stderr_log(f"[probe] saved: {out_path}")
    stderr_log(f"[probe] measured_min_date={result['measured_min_date']} measured_max_date={result['measured_max_date']}")
    stderr_log(f"[統計] APIリクエスト総数={client.request_count} 429リトライ={client.retry_count}")
    return result


def _do_get(api_key: str, endpoint: str, date_iso: str) -> tuple[int, list | None, str]:
    import ssl

    url = f"https://api.jquants.com/v2{endpoint}?date={date_iso}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("x-api-key", api_key)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return resp.status, data.get("data", []), ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        try:
            detail = json.loads(detail).get("message", detail)
        except Exception:
            pass
        return e.code, None, detail
    except Exception as e:  # noqa: BLE001
        return 0, None, f"{type(e).__name__}: {e}"


def fetch_by_date_concurrent(endpoint: str, out_subdir: str, workers: int = 8, force: bool = False) -> None:
    """D-1/D-4/D-5相当: 全営業日を並列（スレッドプール）で取得する。

    合計スループットは RollingRateLimiter により120件/分以内に抑える。
    """
    import os

    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise RuntimeError("JQUANTS_API_KEY が環境変数に見つからない")

    out_dir = RAW_DIR / out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = RAW_DIR / f"{out_subdir}_fetch_state.json"
    state = load_state(state_path)
    done = set(state.get("done_dates", []))
    zero = set(state.get("zero_dates", []))
    out_of_range = set(state.get("out_of_range_dates", []))
    failed = set(state.get("failed_dates", []))

    end = today_jst() - dt.timedelta(days=1)
    days = list(business_days(SCAN_START, end))
    todo = [d.isoformat() for d in days if d.isoformat() not in done and d.isoformat() not in zero and d.isoformat() not in out_of_range]
    stderr_log(f"[{out_subdir}] 走査対象平日数: {len(days)}（未処理: {len(todo)}）")

    limiter = RollingRateLimiter(max_per_minute=110)
    lock = threading.Lock()
    counters = {"n": 0, "retry": 0}
    t0 = time.time()

    def worker(iso: str) -> None:
        backoff = 8.0
        for attempt in range(6):
            limiter.acquire()
            with lock:
                counters["attempts"] = counters.get("attempts", 0) + 1
                if counters["attempts"] % 100 == 0:
                    stderr_log(
                        f"[{out_subdir}] 試行数={counters['attempts']} 成功={counters['n']} "
                        f"接続エラー累計={counters.get('conn_err', 0)}"
                    )
            status, records, err = _do_get(api_key, endpoint, iso)
            if status == 429:
                with lock:
                    counters["retry"] += 1
                time.sleep(backoff)
                backoff *= 2.0
                continue
            if status == 0:
                # 接続エラー（一時的な切断等）。短い待機でリトライする（真の欠測と混同しない）。
                with lock:
                    counters["retry"] += 1
                    counters["conn_err"] = counters.get("conn_err", 0) + 1
                    if counters["conn_err"] % 20 == 0:
                        stderr_log(f"[{out_subdir}] {iso}: 接続エラー累計{counters['conn_err']}件目 (attempt={attempt}): {err}")
                time.sleep(2.0 + attempt * 2.0)
                continue
            if status == 400:
                with lock:
                    out_of_range.add(iso)
                return
            if status != 200:
                with lock:
                    failed.add(iso)
                stderr_log(f"[{out_subdir}] {iso}: エラー status={status} {err}")
                return
            if not records:
                with lock:
                    zero.add(iso)
                return
            out_path = out_dir / f"{iso}.json"
            out_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            with lock:
                done.add(iso)
                failed.discard(iso)
                counters["n"] += 1
                if counters["n"] % 100 == 0:
                    elapsed = time.time() - t0
                    stderr_log(
                        f"[{out_subdir}] 進捗 新規={counters['n']}/{len(todo)} "
                        f"累計成功={len(done)} 0件={len(zero)} 圏外={len(out_of_range)} 失敗={len(failed)} "
                        f"経過={elapsed:.0f}秒"
                    )
                    save_state(state_path, {"done_dates": sorted(done), "zero_dates": sorted(zero), "out_of_range_dates": sorted(out_of_range), "failed_dates": sorted(failed)})
            return
        with lock:
            failed.add(iso)
        stderr_log(f"[{out_subdir}] {iso}: 429リトライ上限到達")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(worker, iso) for iso in todo]
        for f in as_completed(futures):
            f.result()

    save_state(state_path, {"done_dates": sorted(done), "zero_dates": sorted(zero), "out_of_range_dates": sorted(out_of_range), "failed_dates": sorted(failed)})
    stderr_log(
        f"[{out_subdir}] 完了。新規取得={counters['n']} 累計成功={len(done)} 0件日={len(zero)} "
        f"圏外={len(out_of_range)} 失敗={len(failed)} 429リトライ回数={counters['retry']} "
        f"総経過={time.time()-t0:.0f}秒"
    )
    if failed:
        stderr_log(f"[{out_subdir}] 警告: 失敗日が残っている: {sorted(failed)}")


def fetch_by_date_generic(endpoint: str, out_subdir: str, force: bool = False, min_interval: float = 0.55) -> None:
    """全営業日を走査し、1日1リクエストで /equities/bars/daily 等をまとめて取得する（D-1相当の汎用版）。"""
    out_dir = RAW_DIR / out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = RAW_DIR / f"{out_subdir}_fetch_state.json"
    state = load_state(state_path)
    done = set(state.get("done_dates", []))
    zero = set(state.get("zero_dates", []))
    out_of_range = set(state.get("out_of_range_dates", []))
    failed = set(state.get("failed_dates", []))

    end = today_jst() - dt.timedelta(days=1)
    days = list(business_days(SCAN_START, end))
    stderr_log(f"[{out_subdir}] 走査対象平日数: {len(days)}（{SCAN_START} 〜 {end}）")

    client = JQuantsClient(log_fn=stderr_log, min_interval=min_interval)
    n_new = 0
    for i, day in enumerate(days, 1):
        iso = day.isoformat()
        if iso in done or iso in zero or iso in out_of_range:
            continue
        if iso in failed and not force:
            pass  # 再試行する
        out_path = out_dir / f"{iso}.json"
        try:
            body = client.get(endpoint, {"date": iso})
        except RateLimitExhaustedError as e:
            stderr_log(f"[{out_subdir}] {iso}: 429上限到達。失敗日として記録: {e}")
            failed.add(iso)
            save_state(state_path, {"done_dates": sorted(done), "zero_dates": sorted(zero), "out_of_range_dates": sorted(out_of_range), "failed_dates": sorted(failed)})
            continue
        except RuntimeError as e:
            msg = str(e)
            if "HTTP 400" in msg:
                out_of_range.add(iso)
                save_state(state_path, {"done_dates": sorted(done), "zero_dates": sorted(zero), "out_of_range_dates": sorted(out_of_range), "failed_dates": sorted(failed)})
                continue
            stderr_log(f"[{out_subdir}] {iso}: 取得エラー。失敗日として記録: {e}")
            failed.add(iso)
            save_state(state_path, {"done_dates": sorted(done), "zero_dates": sorted(zero), "out_of_range_dates": sorted(out_of_range), "failed_dates": sorted(failed)})
            continue

        records = body.get("data", []) if isinstance(body, dict) else []
        if not records:
            zero.add(iso)
        else:
            out_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            done.add(iso)
            n_new += 1
        failed.discard(iso)
        if i % 50 == 0 or i == len(days):
            stderr_log(f"[{out_subdir}] 進捗 {i}/{len(days)}（新規={n_new} 完了累計={len(done)} 0件={len(zero)} 圏外={len(out_of_range)} 失敗={len(failed)}）")
        save_state(state_path, {"done_dates": sorted(done), "zero_dates": sorted(zero), "out_of_range_dates": sorted(out_of_range), "failed_dates": sorted(failed)})

    stderr_log(f"[{out_subdir}] 完了。新規取得={n_new} 累計成功={len(done)} 0件日={len(zero)} 圏外={len(out_of_range)} 失敗={len(failed)}")
    if failed:
        stderr_log(f"[{out_subdir}] 警告: 失敗日が残っている: {sorted(failed)}")
    stderr_log(f"[統計:{out_subdir}] APIリクエスト総数={client.request_count} 429リトライ={client.retry_count}")


def step_master(dates_file: str, force: bool = False) -> None:
    dates = json.loads(Path(dates_file).read_text(encoding="utf-8"))
    if isinstance(dates, dict):
        dates = dates.get("dates", [])
    out_dir = RAW_DIR / "master_by_date"
    out_dir.mkdir(parents=True, exist_ok=True)
    client = JQuantsClient(log_fn=stderr_log, min_interval=0.6)
    results = []
    for d in dates:
        out_path = out_dir / f"{d}.json"
        if out_path.exists() and not force:
            stderr_log(f"[master] 既存キャッシュを使用: {d}")
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            results.append({"date": d, "status": "cached", "record_count": len(existing)})
            continue
        try:
            body = client.get("/equities/master", {"date": d})
            records = body.get("data", [])
            if records:
                out_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
                results.append({"date": d, "status": "ok", "record_count": len(records)})
                stderr_log(f"[master] 取得: {d}（{len(records)}件）")
            else:
                results.append({"date": d, "status": "zero_records"})
                stderr_log(f"[master] {d}: 0件（point-in-timeマスタが返らない可能性。10Y-COMMON §5.3 フォールバック要検討）")
        except RuntimeError as e:
            results.append({"date": d, "status": "error", "detail": str(e)[:300]})
            stderr_log(f"[master] {d}: エラー {e}")
    summary_path = RAW_DIR / "d3_master_fetch_summary.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    stderr_log(f"[master] summary saved: {summary_path}")
    stderr_log(f"[統計:master] APIリクエスト総数={client.request_count} 429リトライ={client.retry_count}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", required=True, choices=["probe", "bars", "fins", "earnings", "master"])
    parser.add_argument("--dates-file", type=str, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if args.step == "probe":
        step_probe()
    elif args.step == "bars":
        fetch_by_date_concurrent("/equities/bars/daily", "bars_by_date", workers=8, force=args.force)
    elif args.step == "fins":
        fetch_by_date_concurrent("/fins/summary", "fins_summary_by_date", workers=20, force=args.force)
    elif args.step == "earnings":
        fetch_by_date_concurrent("/fins/earnings-date", "earnings_date_by_date", workers=20, force=args.force)
    elif args.step == "master":
        if not args.dates_file:
            print("エラー: --step master には --dates-file が必要", file=sys.stderr)
            return 1
        step_master(args.dates_file, force=args.force)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
