#!/usr/bin/env python3
"""kabu STATION API 疎通のみを素早く確認するスクリプト（タスク 0-8 単体）。

J-Quants側の実測（scripts/step0_api_probe.py）はレート制限対策で数分かかることがある。
kabu STATION の疎通確認自体は本来1〜2回のリクエストで完結する話なので、
J-Quants の実測を待たずに、こちらだけ先に・数秒で確認したいときに使う。

使い方:
    python scripts/check_kabu_station.py

    依存パッケージ不要（Python 3.11+ 標準ライブラリのみ）。
    kabu STATION（Windows常駐アプリ）を起動した状態で実行すること。

.env.local に必要な変数:
    KABU_API_PASSWORD=...   # kabu STATION の「APIシステム設定」で設定したパスワード
    KABU_API_MODE=sandbox   # sandbox(18081・検証) / production(18080・本番)

セキュリティ方針（CLAUDE.md 準拠）: 認証情報の値は一切出力しない。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step0_api_probe import ENV_FILE, load_env_local, probe_kabu  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  古い環境では無視してよい
            pass

    env = load_env_local()
    if not env:
        print(f"⚠️ `{ENV_FILE}` が見つからないか空。KABU_API_PASSWORD を設定すること。")
        return 1

    log: list[str] = []
    probe_kabu(env, log)
    print("\n".join(log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
