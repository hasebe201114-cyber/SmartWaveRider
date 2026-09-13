# STEP0 API 実機疎通レポート（タスク 0-8 / 0-9）

- 実行日時: 2026-09-13 13:32
- 実行環境の Python: 3.14.4
- 対象: J-Quants API **V2**（V1 は 2026-06-01 廃止済みのため対象外）
- **本レポートに認証情報は一切含まれない**（値は長さのみ記録）

## 0-9: J-Quants API (V2) 疎通

### 認証情報の検出状況

- `JQUANTS_API_KEY`: (設定あり・43文字・値は非表示)

### 0. APIキーの有効性チェック

- `/equities/bars/daily`（2026-09-13）への疎通: HTTP 400（**APIキーは有効**。クエリ日付が契約範囲外だっただけ）
- **契約がカバーする日付範囲: 2024-06-21 〜 2026-06-21**
- ⚠️ 契約終了日（2026-06-21）が実行日（2026-09-13）より過去。契約期間が既に終了しているか、期限が固定された過去ログ用プランの可能性がある。

- 契約範囲内の日付（2026-06-19）で再テスト: HTTP 200
- レスポンス構造: `data=list[1]先頭要素keys=(Date,Code,O,H,L,C,UL,LL,Vo,Va)`
- **実データあり**

### 1. エンドポイント別のアクセス可否（＝契約プランで何が使えるか）

（日付パラメータは契約範囲内で疎通確認済みの `2026-06-19`、銘柄コードは `72030` を使用）

| エンドポイント | 用途 | 結果 |
|---|---|---|
| `/equities/master`（推測パス） | 上場銘柄一覧（ユニバース構築の土台） | ✅ 利用可（4443件） |
| `/equities/bars/daily` | 日足 四本値【確認済みパス】 | ✅ 利用可（1件） |
| `/fins/summary`（推測パス） | 財務情報（PEAD のサプライズ度算出に使う） | ✅ 利用可（8件） |
| `/equities/earnings-calendar`（推測パス） | 決算発表予定（H-3 の決算跨ぎ回避に必須） | ✅ 利用可（1件） |
| `/indices/bars/daily`（推測パス） | TOPIX等 指数 日足 | 🚫 プラン制限または権限なし（HTTP 403） |
| `/markets/trading-by-type`（推測パス） | 投資部門別売買状況（需給スリーブ） | 🚫 プラン制限または権限なし（HTTP 403） |
| `/markets/margin-interest`（推測パス） | 信用残（需給スリーブ） | 🚫 プラン制限または権限なし（HTTP 403） |
| `/markets/short-selling`（推測パス） | 業種別空売り比率 | 🚫 プラン制限または権限なし（HTTP 403） |

### 2. 分足・時間足データの有無（重大論点 C-4 の決着材料）

以下は**推測パスの総当たり**である。1つでも 200（かつ実データあり）が返れば、それが正式な分足エンドポイントである可能性が高い。全滅した場合、少なくとも本スクリプトが試した範囲では分足の提供を確認できなかったことを意味する（正式パスがまだ特定できていない可能性は残る）。

| エンドポイント（推測） | 結果 |
|---|---|
| `/equities/bars/minute` | 🚫 HTTP 403（パスは存在するがプラン外の可能性） |
| `/equities/bars/intraday` | 🚫 HTTP 403（パスは存在するがプラン外の可能性） |
| `/equities/bars/1m` | 🚫 HTTP 403（パスは存在するがプラン外の可能性） |
| `/equities/bars/hourly` | 🚫 HTTP 403（パスは存在するがプラン外の可能性） |
| `/equities/bars/am` | 🚫 HTTP 403（パスは存在するがプラン外の可能性） |
| `/equities/prices/am` | 🚫 HTTP 403（パスは存在するがプラン外の可能性） |
| `/equities/prices/minute` | 🚫 HTTP 403（パスは存在するがプラン外の可能性） |

**→ 推測した範囲では分足/時間足エンドポイントを発見できなかった。**ただし本スクリプトのパス推測が外れているだけの可能性があるため、J-Quants マイページの API リファレンス（ログイン後に閲覧可能）で分足関連エンドポイントの掲載有無を目視確認することを推奨する。

### 3. 日足の遡及可能範囲（PJ000001 §6.2 の選定/確認分割が成立するか）

契約がカバーする日付範囲: **2024-06-21 〜 2026-06-21**（0番の結果より）。銘柄コードは `72030`（0番で実データが確認できたもの）を使用し、この範囲内で月次に実測する。

| 日付 | データ有無 |
|---|---|
| 2024-06-21 | — なし（HTTP 429・`(空またはJSON以外)`） |
| 2024-07-01 | — なし（HTTP 429・`(空またはJSON以外)`） |
| 2024-08-01 | — なし（HTTP 429・`(空またはJSON以外)`） |
| 2024-09-02 | — なし |
| 2024-10-01 | — なし |
| 2024-11-01 | — なし |
| 2024-12-02 | — なし |
| 2025-01-01 | — なし |
| 2025-02-03 | — なし |
| 2025-03-03 | — なし |
| 2025-04-01 | — なし |
| 2025-05-01 | — なし |
| 2025-06-02 | — なし |
| 2025-07-01 | — なし |
| 2025-08-01 | — なし |
| 2025-09-01 | — なし |
| 2025-10-01 | — なし |
| 2025-11-03 | — なし |
| 2025-12-01 | — なし |
| 2026-01-01 | — なし |
| 2026-02-02 | — なし |
| 2026-03-02 | — なし |
| 2026-04-01 | — なし |
| 2026-05-01 | — なし |
| 2026-06-01 | — なし |

**実測できた最も古い日付: 2024-06-21**
→ **選定期間 2015-2022 は確保できない**（契約は 2024-06-21 までしか遡れない）。PJ000001 §6.2 の選定/確認分割は、この契約の下では成立しない。確認期間のみのフォワード中心の検証、または有料プランの検討が必要。

### 4. データ遅延の実測（無料プランは12週間遅延とされる）

- 探索範囲内に取得できるデータが見つからなかった。

### 5. `/fins/summary` のフィールド構造（PEAD prescreen R-1: 会社業績予想の有無）

- レスポンス構造: `data=list[8]先頭要素keys=(DiscDate,DiscTime,Code,DiscNo,DocType,CurPerType,CurPerSt,CurPerEn,CurFYSt,CurFYEn)`
- 1レコードの全フィールド名: `DiscDate, DiscTime, Code, DiscNo, DocType, CurPerType, CurPerSt, CurPerEn, CurFYSt, CurFYEn, NxtFYSt, NxtFYEn, Sales, OP, OdP, NP, EPS, DEPS, TA, Eq, EqAR, BPS, CFO, CFI, CFF, CashEq, Div1Q, Div2Q, Div3Q, DivFY, DivAnn, DivUnit, DivTotalAnn, PayoutRatioAnn, FDiv1Q, FDiv2Q, FDiv3Q, FDivFY, FDivAnn, FDivUnit, FDivTotalAnn, FPayoutRatioAnn, NxFDiv1Q, NxFDiv2Q, NxFDiv3Q, NxFDivFY, NxFDivAnn, NxFDivUnit, NxFPayoutRatioAnn, FSales2Q, FOP2Q, FOdP2Q, FNP2Q, FEPS2Q, NxFSales2Q, NxFOP2Q, NxFOdP2Q, NxFNp2Q, NxFEPS2Q, FSales, FOP, FOdP, FNP, FEPS, NxFSales, NxFOP, NxFOdP, NxFNp, NxFEPS, MatChgSub, SigChgInC, ChgByASRev, ChgNoASRev, ChgAcEst, RetroRst, ShOutFY, TrShFY, AvgSh, NCSales, NCOP, NCOdP, NCNP, NCEPS, NCTA, NCEq, NCEqAR, NCBPS, FNCSales2Q, FNCOP2Q, FNCOdP2Q, FNCNP2Q, FNCEPS2Q, NxFNCSales2Q, NxFNCOP2Q, NxFNCOdP2Q, NxFNCNP2Q, NxFNCEPS2Q, FNCSales, FNCOP, FNCOdP, FNCNP, FNCEPS, NxFNCSales, NxFNCOP, NxFNCOdP, NxFNCNP, NxFNCEPS, ShEq, NCShEq, ROE, NCROE`
- 上記に「Forecast」「予想」「会社予想」に相当するフィールド（例: ForecastNetSales, ForecastOperatingProfit 等）が含まれるか目視確認すること。含まれていれば R-1 は解消、含まれていなければ SUE が算出できず PEAD は成立しない

### 6. `/equities/master` のフィールド構造（PEAD prescreen R-1c: ユニバース定義）

- 取得失敗（HTTP 429 / Rate limit exceeded. Please try again later.）。R-1c は未確認のまま。

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
