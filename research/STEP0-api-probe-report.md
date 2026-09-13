# STEP0 API 実機疎通レポート（タスク 0-8 / 0-9）

- 実行日時: 2026-09-13 11:52
- 実行環境の Python: 3.14.4
- 対象: J-Quants API **V2**（V1 は 2026-06-01 廃止済みのため対象外）
- **本レポートに認証情報は一切含まれない**（値は長さのみ記録）

## 0-9: J-Quants API (V2) 疎通

### 認証情報の検出状況

- `JQUANTS_API_KEY`: (設定あり・43文字・値は非表示)

### 0. APIキーの有効性チェック

- `/equities/bars/daily` への疎通: **予期しない結果**（HTTP 400 / Your subscription covers the following dates: 2024-06-21 ~ 2026-06-21. If you want more data, please check other plans:h）

APIキーが無効と判定されたため、以降のプローブは実施しなかった。
J-Quants マイページでキーの発行状態・契約プランを確認すること。

## 0-8: kabu STATION API 疎通

- モード: `sandbox` → ポート **18081**
- localhost:18081 への接続: **失敗**（ConnectionRefusedError）
- → kabu STATION（Windows常駐アプリ）が起動していないか、API 利用設定が有効になっていない。
- **これは PJ000001 §4 H-2 が指摘した単一障害点そのもの**。常駐PCが落ちていれば取引も停止する。

---

## 次のアクション

1. 本レポートを `research/STEP0-api-probe-report.md` としてコミットする
2. 「3. 日足の遡及可能範囲」と「2. 分足・時間足データの有無」の結果をもとに、
   `research/ACTIVE.md` のタスク 0-9 を完了、0-13（C-4）を決着させる
3. C-4 の選択肢1〜3のどれを採るかは司令塔判断とする
4. 推測パスが的中しなかった場合、J-Quants マイページの API リファレンスで
   正式なエンドポイント名を目視確認し、本スクリプトの候補リストを更新する
