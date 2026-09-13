#!/usr/bin/env python3
"""`/equities/master` のフィールド構造のみを単体で確認するスクリプト（PEAD prescreen R-1c）。

R-1c（TOPIX500構成銘柄を識別できるフィールドがあるか）は、step0_api_probe.py の
フル実行では毎回 §6（最後の方）で HTTP 429（レート制限）に阻まれ、
複数回試しても一度も成功していない。他のプローブを一切叩かず、
「有効性チェック→master確認」の最小2リクエストだけに絞ることで、
レート制限に当たる前に確認を終えることを狙う。

使い方:
    python scripts/check_master_schema.py

    依存パッケージ不要（Python 3.11+ 標準ライブラリのみ）。

.env.local に必要な変数:
    JQUANTS_API_KEY=...     # J-Quants マイページ（ダッシュボード）で発行した V2 用 APIキー

セキュリティ方針（CLAUDE.md 準拠）: 認証情報の値は一切出力しない。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step0_api_probe import (  # noqa: E402
    ENV_FILE,
    get_api_key,
    load_env_local,
    probe_master_schema,
    sanity_check,
)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  古い環境では無視してよい
            pass

    print("R-1c（/equities/master のフィールド構造）だけを確認します。", flush=True)

    env = load_env_local()
    if not env:
        print(f"⚠️ `{ENV_FILE}` が見つからないか空。JQUANTS_API_KEY を設定すること。")
        return 1

    log: list[str] = []
    api_key = get_api_key(env, log)
    if not api_key:
        print("\n".join(log))
        return 1

    print("[1/2] APIキーの有効性チェック中...", flush=True)
    ok, _sub_start, _sub_end, _valid_date, valid_code = sanity_check(api_key, log)
    if not ok:
        print("\n".join(log))
        print("有効な日付が特定できなかったため、master確認を実施できなかった。")
        return 1

    print(f"[2/2] /equities/master のフィールド構造を確認中... (銘柄コード: {valid_code})", flush=True)
    probe_master_schema(api_key, valid_code, log)

    print("\n".join(log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
