# Smart Wave Rider - GitHub リポジトリセットアップ手順

**本ドキュメント**: ローカルで作成した Smart Wave Rider プロジェクトを GitHub にプッシュするための手順です。

---

## ステップ 1: GitHub でリポジトリを作成

### 1.1 GitHub にログイン
https://github.com/ にアクセス→ログイン

### 1.2 新しいリポジトリを作成
- 右上の「+」→「New repository」をクリック
- **Repository name**: `smart-wave-rider`
- **Description**: `日本株AI自動売買EA（Expert Advisor）システム`
- **Visibility**: Private（推奨・機密情報含むため）
- **Initialize this repository with**: **チェックしない**（既に README.md が存在するため）
- 「Create repository」をクリック

### 1.3 リポジトリ URL をコピー
リモートリポジトリの URL をコピーします（以下のいずれか）：

**HTTPS**: `https://github.com/hasebe201114-cyber/smart-wave-rider.git`  
**SSH**: `git@github.com:hasebe201114-cyber/smart-wave-rider.git`

---

## ステップ 2: ローカルリポジトリをリモートに接続

ターミナルで以下を実行：

```bash
cd /path/to/smart-wave-rider

# リモートリポジトリを追加
git remote add origin https://github.com/hasebe201114-cyber/smart-wave-rider.git

# 接続確認
git remote -v
# 出力例:
# origin  https://github.com/hasebe201114-cyber/smart-wave-rider.git (fetch)
# origin  https://github.com/hasebe201114-cyber/smart-wave-rider.git (push)
```

---

## ステップ 3: GitHub にプッシュ

### 3.1 ブランチ名を確認
```bash
git branch -M main
# master → main にリネーム（GitHub の最新規約に合わせる）
```

### 3.2 プッシュ実行
```bash
git push -u origin main
# -u オプションで upstream を設定（次回以降は git push のみで OK）
```

**初回のみ認証が求められる場合**:
- HTTPS の場合: GitHub 上で生成した Personal Access Token (PAT) を使用
- SSH の場合: SSH キーペアを設定済みであれば自動

### 3.3 プッシュ完了確認
```bash
# GitHub のブラウザで確認
# https://github.com/hasebe201114-cyber/smart-wave-rider

# または CLI で確認
git log origin/main -n 1
```

---

## ステップ 4: GitHub での設定確認

### 4.1 リポジトリ設定を確認
GitHub リポジトリページ → Settings → General で以下を確認：
- [ ] Repository name: `smart-wave-rider`
- [ ] Visibility: Private
- [ ] Default branch: `main`

### 4.2 .gitignore が有効か確認
Settings → Code security & analysis で以下を確認：
- [ ] `Dependabot alerts`: 有効にすることを推奨

### 4.3 リポジトリ URL を記録
プロジェクト関係者に共有：
```
リポジトリ: https://github.com/hasebe201114-cyber/smart-wave-rider
メインブランチ: main
```

---

## ステップ 5: 開発環境の初期化（ローカル・Claude Code）

### 5.1 仮想環境構築
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 5.2 環境変数設定
```bash
cp .env.example .env.local
# .env.local を編集して API キーを設定
```

### 5.3 テスト実行（オプション）
```bash
pytest tests/ -v
# テストが成功することを確認
```

---

## ステップ 6: Claude Code での開発開始

### 6.1 Claude Code でプロジェクトを開く
```bash
cd /path/to/smart-wave-rider
claude code . --editor vscode
```

### 6.2 STEP1 から開始
```bash
# STEP1: 戦略検討フェーズ
python scripts/step1_screening.py
```

---

## 今後の開発フロー

### 定期的な commit
```bash
# 変更確認
git status

# 変更をステージング
git add .

# コミット（STEP 情報を含める）
git commit -m "STEP1: Create technical indicator validation script"

# プッシュ
git push origin main
```

### ブランチ運用（オプション）
本格開発時は機能ごとにブランチを作成することを推奨：

```bash
# 新しいブランチ作成
git checkout -b feature/step2-backtest

# 開発実施
# ...

# main にマージ前に Pull Request (PR) を作成（コードレビュー用）
git push origin feature/step2-backtest
# GitHub で PR を作成
```

---

## トラブルシューティング

### エラー: "fatal: not a git repository"
```bash
# Git リポジトリが初期化されていない
git init
git remote add origin <URL>
```

### エラー: "Permission denied (publickey)"
```bash
# SSH キーが設定されていない場合は HTTPS を使用
git remote set-url origin https://github.com/hasebe201114-cyber/smart-wave-rider.git
```

### エラー: ".env.local がコミットされてしまった"
```bash
# Git の履歴から削除（重要：本番前に実施）
git rm --cached .env.local
git commit -m "Remove .env.local from tracking"
git push origin main

# GitHub Settings → Security → Secret scanning で詳細を確認
```

---

## チェックリスト

```markdown
## GitHub リポジトリセットアップ完了確認

### リモートリポジトリ
- [ ] GitHub で `smart-wave-rider` リポジトリを作成した
- [ ] Visibility を Private に設定した
- [ ] リモート URL を確認した

### ローカルリポジトリ接続
- [ ] `git remote add origin` を実行した
- [ ] `git remote -v` で接続を確認した
- [ ] メインブランチを `main` にリネームした

### プッシュ
- [ ] `git push -u origin main` を実行した
- [ ] GitHub ブラウザでファイルが表示されることを確認した
- [ ] コミット履歴が表示されることを確認した

### 開発環境
- [ ] 仮想環境を作成した
- [ ] `pip install -r requirements.txt` を実行した
- [ ] `.env.local` に API キーを設定した
- [ ] `pytest` でテストが実行できることを確認した

### Claude Code
- [ ] `claude code . --editor vscode` で開くことができた
- [ ] STEP1 スクリプトが実行可能な状態か確認した
- [ ] README.md, CLAUDE.md が文字化けなく表示されるか確認した
```

---

## 次のステップ

GitHub セットアップが完了したら：

1. **STEP1 実装開始**
   - `research/STEP1-planning.md` を参照
   - `scripts/step1_screening.py` の実装開始

2. **週次進捗更新**
   - `research/ACTIVE.md` を作成して進捗を記録
   - `research/STRATEGY-BRIEF.md` で戦略概要を定期更新

3. **定期 commit**
   - 区切りの良いところで GitHub に push
   - PR・コードレビュー運用を導入（チーム開発時）

---

**作成日**: 2026-09-13  
**対象版本**: Smart Wave Rider v0.1.0
