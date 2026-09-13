# research/ - エージェント間バケツリレー作業場

このフォルダは、マルチエージェント体制が実験を手渡しで進めるための**作業場**です。
正式な記録は `obs/smart_wave_rider/` 側が「正」であり、ここは結論が出たら破棄してよい**揮発領域**です。

詳細な運営ルールは
`obs/smart_wave_rider/00プロジェクト方針/PJ000002-マルチエージェント運営計画書.md`
を参照してください（未作成・STEP0 で整備）。

## 構成

```
research/
├─ README.md              ← このファイル
├─ ACTIVE.md              ← E管理：進行状況の信号機（誰待ちか）
├─ STRATEGY-BRIEF.md      ← S管理：作戦ブリーフ（勝敗・停滞・次の一手）
├─ portfolio-ledger.md    ← S管理：採用/不採用/保留の台帳
├─ STEP1-planning.md      ← 初期の STEP1 計画（PJ000001 起票前の資料・要改訂）
├─ config/
│  └─ parameter-set.json  ← 確定パラメータ（STEP2 で確定）
├─ _templates/            ← 各成果物の雛形（ブレ防止）
│  ├─ prescreen.template.md
│  ├─ spec.template.md
│  ├─ result.readme.md
│  ├─ review.template.md
│  └─ decision.template.md
└─ EXP-OBSxxxxx/          ← 実験1件＝フォルダ1つ＝OBS番号と一致
   ├─ 00-prescreen.md     ← S：目利きゲート
   ├─ 00-spec.md          ← A：実験仕様（成功基準を回す前に数値で固定）
   ├─ 10-result/          ← B：生データのみ（解釈・採否は書かない）
   ├─ 20-review.md        ← C：採用/不採用判定
   └─ 30-decision.md      ← D/E：反映 or 棚卸し記録
```

## 実験の作り方（新規時）

1. E進行チームが次の OBS 連番を採番し、`EXP-OBSxxxxx/` を作成。
2. `_templates/` の雛形をコピーして各成果物を埋めていく。
3. 結論確定後、E が要約を OBS 件名へ吸い上げ、`EXP-OBSxxxxx/` は破棄してよい。

## 鉄則

- **成功基準は「回す前」に数値で確定する**（HARKing 防止）
- **予測単位とパイプライン統合の両方を測定する**（片方だけは禁止）
- **B（実装）と C（判定）は必ず別エージェント**とし、自分の実装を甘く採点させない
- **採用ゲートは人間**。C の「採用可」は推薦であり、本番反映は司令塔の最終GOを必須とする
- **不採用も負の結果として `portfolio-ledger.md` に残す**（偽陽性回避の資産）
