# EXP-OBS000007 0-25 信用取引残高 一括取得サマリー

> 担当: B実装チーム（quant-researcher）
> 位置づけ: **データ保全のみ**。エッジ検定・spec起票・G1測定は一切含まない（判定語・解釈なし）。
> 実行: `research/ACTIVE.md` 0-25。S戦略チームの条件付きGO判定（`00-prescreen.md` §2.1）を受け、
> J-Quants Standardプラン契約の時限運用（1ヶ月以内に完了・月末までに解約）のため、
> ローリング窓が動く前に候補ユニバース554銘柄の信用取引残高10年分を`data/raw/`へ永続化した。

## 変更履歴

- 2026-09-16: 新規作成。0-25実行の一環として554銘柄の信用取引残高取得完了を記録。

## 実行環境

- 実行日: 2026-09-16
- スクリプト: `scripts/margin_fetch_data.py`（新規作成。`scripts/pead_fetch_data.py` / `scripts/gap_fetch_data.py` と同型のレジューム可能パターン）
- 候補コードリスト: `research/EXP-OBS000005/10-result/candidate_codes_v2.json`（554銘柄）
- 出力先: `data/raw/margin_interest/{code}.json`（銘柄ごとの生レコードリスト）
- レジューム状態ファイル: `data/raw/margin_interest/margin_fetch_state.json`
- レート制御: `JQuantsClient`既定（0.6秒間隔、Standardプラン120件/分に対する安全マージン）

## 実行手順（再現用コマンド）

```bash
cd /home/user/SmartWaveRider
export JQUANTS_API_KEY=（マイページ発行のAPIキー）
# 1回目実行
python3 scripts/margin_fetch_data.py --codes-file research/EXP-OBS000005/10-result/candidate_codes_v2.json \
  > /tmp/margin_fetch_stdout.log 2> research/EXP-OBS000007/10-result/run_margin_fetch.log

# 1回目で接続エラー（HTTP 0, Connection reset by peer）により3銘柄が失敗したため、
# レジューム機構により失敗銘柄のみ自動的に再試行する2回目実行（--forceは使わない）
python3 scripts/margin_fetch_data.py --codes-file research/EXP-OBS000005/10-result/candidate_codes_v2.json \
  >> /tmp/margin_fetch_stdout.log 2>> research/EXP-OBS000007/10-result/run_margin_fetch.log
```

実行ログ全文: [`run_margin_fetch.log`](run_margin_fetch.log)

## 取得結果

| 項目 | 値 |
|---|---|
| 対象銘柄数 | 554 |
| 成功銘柄数（1回目） | 551（99.5%） |
| 失敗銘柄数（1回目） | 3（`41860`・`46130`・`84100`。いずれも`HTTP 0 - 接続失敗: [Errno 104] Connection reset by peer`。429（レート制限）ではなく一過性の接続断） |
| 成功銘柄数（2回目・レジューム後） | 554/554（100%） |
| 最終失敗銘柄数 | 0 |
| 0件応答（実データなし）銘柄数 | 0 |
| 総リクエスト数 | 554（1回目）+ 3（2回目・失敗銘柄のみ再試行） = 557 |
| 429（レート制限）発生回数 | 0 |
| 総ファイル数（`data/raw/margin_interest/*.json`、state除く） | 554 |
| 総レコード数（全554銘柄合計） | 270,081件 |
| ディスク使用量 | 約51MB |

## データ期間の分布

| 項目 | 値 |
|---|---|
| 最頻レコード数/銘柄 | 507件（491/554銘柄でこの値） |
| 最も早い「最古レコード日」 | 2016-09-23 |
| 最も遅い「最古レコード日」 | 2021-04-02 |
| 最古レコード日の分布 | `2016-09-23`: 545銘柄／それ以外（`2016-11-25`・`2017-03-31`・`2017-09-29`・`2021-04-02`）: 各1銘柄 |
| 最も早い「最新レコード日」 | 2017-03-31 |
| 最も遅い「最新レコード日」 | 2026-09-11 |
| 最新レコード日の分布（上位） | `2026-09-11`: 502銘柄／`2021-09-24`: 3銘柄／`2022-09-30`: 3銘柄／`2017-03-31`: 2銘柄／`2025-11-28`: 2銘柄／その他: 42銘柄 |

参考: 0-24プローブ（`margin_interest_probe.md`）でトヨタ自動車（`72030`）単独について実測した取得可能期間は2016-09-16〜2026-09-11。本一括取得はその翌日（2026-09-16終日）に実行しているため、ローリング窓が1日前進している可能性がある（最頻の最古レコード日が2016-09-23＝プローブ実測の1週間後であることと整合。週次データのため境界のずれが1週間単位で現れている可能性があるが、原因の特定は本タスクの範囲外）。最新レコード日が`2026-09-11`より早い銘柄（52銘柄）が存在する事実のみ記録する（原因の特定・解釈は行わない）。

## 既存データへの影響

- `data/raw/pead/`・`data/raw/gap/` は本作業で一切変更していない（実行前後でmtime変化なし、確認済み）。
- 新規ディレクトリ `data/raw/margin_interest/` のみへの書き込み。

## 再現性・決定性についての注記

- 乱数不使用。
- `data/raw/margin_interest/` は`.gitignore`（`data/raw/*`）の対象のためコミット対象外（`data/raw/pead/`・`data/raw/gap/`と同じ扱い）。
- 本エンドポイントは他のJ-Quantsエンドポイント（`/equities/bars/daily`等）と同様にローリング窓であるため、**本キャッシュは実行日（2026-09-16）時点のスナップショットであり、翌日以降に同じ範囲を再取得することはできない**（`research/EXP-OBS000001/11-data-refetch-standard-plan.md`の教訓と同型）。今後の作業では`--force`による上書き・再取得を行わないこと。

## 関連ドキュメント

- [`research/ACTIVE.md`](../../ACTIVE.md) 0-25（本作業の実行計画）
- [`research/EXP-OBS000007/00-prescreen.md`](../00-prescreen.md) §2.1（S戦略チームの条件付きGO判定）
- [`margin_interest_probe.md`](margin_interest_probe.md) / [`margin_interest_probe.json`](margin_interest_probe.json)（0-24実機確認）
- [`run_margin_fetch.log`](run_margin_fetch.log)（本作業の実行ログ）
- `scripts/margin_fetch_data.py`（本作業の取得スクリプト）
