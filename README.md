# Smart Wave Rider

**日本株AI自動売買EA（Expert Advisor）システム**

三菱UFJ eスマート証券のkabu STATION APIを活用したスイングトレード向け自動売買システム。テクニカル分析とファンダメンタル分析（ニュース）をClaude APIで統合し、複数戦略パターンを検証・運用する。

## プロジェクト概要

| 項目 | 値 |
|------|-----|
| **対象市場** | 日本株（日経225銘柄を起点） |
| **取引スタイル** | スイングトレード（3-10営業日保有） |
| **ポジション** | ロング/ショート両対応 |
| **初期資本** | 30万円 |
| **レバレッジ** | 1.5-2.0倍（リスク限定） |
| **期待勝率** | 55-60%（戦略による） |
| **開発環境** | Python 3.11+ / Claude Code |

## クイックスタート

### 環境構築

```bash
# リポジトリクローン
git clone https://github.com/hasebe201114-cyber/smart-wave-rider.git
cd smart-wave-rider

# 仮想環境作成
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 依存ライブラリインストール
pip install -r requirements.txt

# 環境変数設定
cp .env.example .env.local
# .env.local に以下を設定:
# - KABU_API_TOKEN
# - CLAUDE_API_KEY
# - JQUANTS_API_KEY
```

### 開発開始

```bash
# Claude Code で開く
claude code . --editor vscode

# STEP1: 戦略検討フェーズ
python scripts/step1_screening.py

# STEP2: バックテスト
python scripts/backtest_main.py --strategy all --period full

# STEP3: フォワードテスト（サンドボックス）
python scripts/forward_tester.py

# STEP4: 本番運用
python scripts/live_trader.py --mode production
```

## プロジェクト構成

```
smart-wave-rider/
├── README.md                       # このファイル
├── CLAUDE.md                       # Claude Code 開発ルール
├── .env.example                    # 環境変数テンプレート
├── .gitignore
├── requirements.txt                # Python 依存ライブラリ
│
├── src/
│   ├── core/                       # テクニカル分析・AI分析エンジン
│   ├── adapters/                   # API アダプタ（kabu, J-Quants）
│   ├── strategies/                 # 売買戦略実装
│   ├── utils/                      # ユーティリティ
│   └── types/                      # データモデル
│
├── scripts/
│   ├── step1_screening.py          # STEP1: 銘柄スクリーニング
│   ├── step1_technical_backtest.py # STEP1: テクニカル検証
│   ├── backtest_main.py            # STEP2: メインバックテスト
│   ├── backtest_walkforward.py      # STEP2: ウォークフォワード分析
│   ├── forward_tester.py           # STEP3: フォワードテスト
│   ├── live_trader.py              # STEP4: 本番トレード
│   └── lib/                        # スクリプト用共有ライブラリ
│
├── research/
│   ├── STEP1-strategy-brief.md     # STEP1 成果物
│   ├── STEP2-backtest-report.html  # STEP2 成果物
│   ├── STEP3-forward-test-log.md   # STEP3 成果物
│   └── config/
│       └── parameter-set.json      # 最適パラメータ定義
│
├── tests/
│   ├── test_technical_engine.py
│   ├── test_signal_synthesis.py
│   ├── test_kabu_adapter.py
│   └── test_integration.py
│
├── data/
│   ├── raw/                        # 生データ
│   ├── processed/                  # 処理済みデータ
│   └── cache/                      # ローカルキャッシュ
│
└── logs/
    ├── backtest/
    ├── forward_test/
    └── live_trading/
```

## 実装プロセス（STEP別）

### STEP1: 戦略検討 & 簡易シミュレーション（1-2週間）

**対象銘柄スクリーニング、テクニカル指標検証、期待値試算**

```bash
python scripts/step1_screening.py
# 出力: research/STEP1-screening.csv

python scripts/step1_technical_backtest.py
# 出力: research/STEP1-expected-value.xlsx
```

**成果物**:
- `research/STEP1-strategy-brief.md`
- `research/STEP1-screening.csv`
- `research/STEP1-expected-value.xlsx`

### STEP2: 投資戦略の具体化 & バックテスト（3-4週間）

**4パターン戦略の詳細実装・バックテスト・パラメータ最適化**

```bash
# 全戦略をバックテスト
python scripts/backtest_main.py --strategy all --period 2021-2026

# ウォークフォワード分析
python scripts/backtest_walkforward.py

# パラメータ最適化
python scripts/backtest_parameter_optimize.py
```

**4つの戦略パターン**:
- **A: モメンタム追従** (MACD + RSI, 5-10日, 勝率55%)
- **B: 平均回帰** (ボリンジャーバンド, 2-5日, 勝率58%)
- **C: トレンドフォロー** (移動平均クロス, 10-15日, 勝率52%)
- **D: ボラティリティ先行** (ATR, 1-3日, 勝率60%)

**成果物**:
- `research/STEP2-backtest-report.html`
- `research/STEP2-walkforward-analysis.csv`
- `research/STEP2-parameter-optimization.md`
- `src/strategies/main_strategy.py`

### STEP3: フォワードテスト（2-3週間）

**kabu API サンドボックス環境でペーパートレード実施**

```bash
# リアルタイムシミュレーション
python scripts/forward_tester.py
# 営業日 09:00-15:00 リアルタイムで実行
```

**確認項目**:
- API応答遅延（目標: 1-3秒）
- シグナル品質（バックテスト予想値との一致度）
- ポジション管理・リスク制御の動作
- False Positive 率

**成果物**:
- `research/STEP3-forward-test-log.md`
- `research/STEP3-api-latency-report.md`
- `research/STEP3-signal-quality-metrics.csv`

### STEP4: 本番運用（継続）

**実取引開始・段階的ポジション拡大・パフォーマンス改善**

```bash
# 本番トレード開始（毎営業日 08:50 起動）
python scripts/live_trader.py --mode production

# 週次サマリー生成
python scripts/weekly_summary.py
```

**スケジュール**:
- Week 1: 標準サイズの 1/5
- Week 2: 標準サイズの 2/5
- Week 3: 標準サイズの 3/5
- Week 4+: 標準サイズ（最大20万円運用可能）

**成果物**:
- `research/STEP4-live-trading-log.md`
- Firebase Firestore: リアルタイムPnL、ポジション履歴
- Discord/Slack: 日次通知

## マルチ戦略検証の方針

初期要件の「後だしじゃんけん型」から **「マルチ戦略検証型」へシフト**。

以下4つの戦略パターンを STEP2-STEP3 で並行検証し、最も有望な 2パターン を STEP4 本番運用で採用する。

| パターン | テクニカル | 保有期間 | 期待値 | ファンダメンタル |
|---------|----------|---------|--------|---|
| A: モメンタム追従 | MACD + RSI | 5-10日 | 勝率55%, PF1.8 | ニュースサポート |
| B: 平均回帰 | ボリンジャーバンド + CCI | 2-5日 | 勝率58%, PF2.0 | 逆張りニュース |
| C: トレンドフォロー | 移動平均クロス + ADX | 10-15日 | 勝率52%, PF1.6 | 業界トレンド |
| D: ボラティリティ先行 | ATR + Stochastic | 1-3日 | 勝率60%, PF2.1 | 警告ニュース |

## 既存資産の流用

**trading-app-v2（暗号資産版）から以下モジュールを参考：**

- `decision-layer`: テクニカル信号生成（パラメータ日本株向け調整）
- `llm-layer`: Claude API 統合（ニュース分析へ拡張）
- `risk-layer`: ドローダウン・損切制御
- `pattern-engine`: パターン認識フレームワーク
- `pipeline`: ERR 機構、バッチ処理フレームワーク

詳細は `CLAUDE.md` の「既存資産の流用」セクション参照。

## 技術スタック

| レイヤー | 技術 | 用途 |
|---------|------|------|
| **言語** | Python 3.11+ | コアロジック |
| **API** | kabu STATION API | 注文・約定・リアルタイム株価 |
| **データ** | J-Quants API | 日足データ・企業財務 |
| **AI分析** | Claude API (Haiku/Sonnet) | ニュース分析・シグナル合成 |
| **バックテスト** | backtrader / backtest.py | 戦略検証 |
| **スケジュール** | APScheduler | 定期実行・市場時間管理 |
| **データベース** | Firebase Firestore (Optional) | 取引履歴・PnL管理 |

## API 統合

### kabu STATION API

```python
# 認証
POST /v1/token

# リアルタイム株価
GET /v1/board/{symbol}

# 注文発注
POST /v1/orders

# ポジション照会
GET /v1/positions

# 口座照会
GET /v1/account
```

### J-Quants API

```python
# 日足データ
GET /v1/daily

# 企業情報
GET /v1/company
```

### Claude API

```python
# ニュース分析（センチメント、影響度スコアリング）
prompt = "以下のニュースを分析して、株価への影響度を -100 to +100 で評価してください"
```

## パフォーマンス期待値

| 指標 | 期待値 | 備考 |
|------|--------|------|
| **勝率** | 55-60% | 個別戦略による |
| **平均利幅** | 0.8-1.5% / トレード | マーケット環境に依存 |
| **月間リターン** | 3-5% | ボラティリティに依存 |
| **最大DD** | 15% | 許容度上限 |
| **Sharpe比** | 0.8-1.2 | 想定範囲 |

※ STEP2 バックテスト結果に基づき実績値に更新予定

## 開発ルール

**詳細は `CLAUDE.md` を参照。** 要点：

- セキュリティ優先（APIキー・認証情報は .env.local で管理）
- テスト駆動開発（実装前に単体テスト・統合テスト設計）
- ドキュメント重視（実装と同時に docstring, README 更新）
- マルチエージェント体制（検証の独立性確保）

## セットアップ手順

### 1. リポジトリ初期化

```bash
# GitHub でリポジトリ作成（web UI）
# 例: https://github.com/hasebe201114-cyber/smart-wave-rider

git clone https://github.com/hasebe201114-cyber/smart-wave-rider.git
cd smart-wave-rider
```

### 2. 仮想環境構築

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 環境変数設定

```bash
cp .env.example .env.local

# .env.local を編集（APIキー記入）
# - KABU_API_TOKEN: kabu STATION API トークン
# - CLAUDE_API_KEY: Anthropic API キー
# - JQUANTS_API_KEY: J-Quants API キー
```

### 4. ディレクトリ初期化

```bash
mkdir -p data/{raw,processed,cache}
mkdir -p logs/{backtest,forward_test,live_trading}
mkdir -p research/config
```

### 5. テスト実行

```bash
pytest tests/ -v
# すべてのテストが GREEN になることを確認
```

## 実行スケジュール

### 営業日（平日）

```
08:50   システム起動・準備
09:00   市場オープン・シグナル監視開始
11:30   前場終了
12:30   後場開始
15:00   市場クローズ
15:10   日次レポート生成 → Slack/Discord 通知
```

### 定期レビュー

```
毎週金曜 16:00   週次サマリー生成
毎月月末         月次レビュー & パラメータ検討
四半期ごと       戦略見直し
```

## トラブルシューティング

### API接続エラー

```python
# kabu API トークン更新
python scripts/refresh_kabu_token.py

# J-Quants データ取得確認
python scripts/test_jquants_connection.py
```

### シグナルに違和感がある

```bash
# 最新のテクニカル指標を再計算
python scripts/recalculate_indicators.py --symbol 9984

# Claude API 分析ログを確認
tail -f logs/live_trading/claude_analysis.log
```

### ポジション管理の不具合

```bash
# 現在のポジション状況確認
python scripts/check_positions.py

# 証拠金状況確認
python scripts/check_margin.py
```

## 参考資料

- [kabu STATION API ドキュメント](https://kabucom.github.io/docs/api/)
- [J-Quants API ドキュメント](https://jpx.gitbook.io/j-quants-api-docs/)
- [Claude API ドキュメント](https://docs.anthropic.com/)
- [trading-app-v2（参考版）](https://github.com/hasebe201114-cyber/trading-app-v2)

## ライセンス

MIT License

## 連絡先

プロジェクト主体: あつし（Atsushi Hasebe）

---

**最終更新**: 2026-09-13  
**ステータス**: 準備段階（STEP1 開始前）
