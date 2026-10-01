# ops-acceptance-harness

LangGraph job runner for operations work: a worker submits evidence, an independent machine verifier and a separate reviewer decide acceptance.

業務・保守運用の作業を LangGraph の job として管理し、「作業者の自己申告」ではなく「独立した機械検証と別担当のレビュー」で受け入れを決める仕組みです。公開用に整理したサンプル実装です。

## 概要

依頼を受けて AI エージェントに作業をさせるとき、「できました」の一言で完了にすると、存在確認だけで品質を断定する・失敗を握りつぶす、といった事故が起きます。このリポジトリは、作業の受付から完了までを状態機械（LangGraph）で管理し、完了の判定を作業者から切り離します。

## 主な機能・設計のポイント

- **作業と検証の分離**: 作業待ち（interrupt）で止まり、作業者が成果物とそのハッシュを提出すると、グラフが機械検証を別に実行します。合格してもレビューが必要な job は、別担当のレビュー提出を待ちます。
- **差し戻しと上限**: 検証・レビューで不合格なら上限内で作業に戻し、`max_attempts`（1〜5）を超えたら失敗と残作業を保存して止めます。
- **状態の永続化**: LangGraph の SQLite checkpointer で job ごとの状態を保存し、別プロセスの CLI から追跡・再開できます。作業中に落ちても `status` / `continue` で続きから進められます。
- **検証プログラムの固定（verifier pin）**: `job start` の時点で検証側のコード一式を読み取り専用の写しに保存し、その job の以降の操作はすべてその写しで実行します。共通部分を更新しても進行中の job が古くならず、写しを書き換えると不合格になります（`ops_harness/verifier_pins.py`）。
- **読み取り専用の診断（probes）**: 設定ファイルで宣言したパス・ツール・ローカル HTTP・Windows タスクを、副作用なしで確認します。HTTP はリダイレクトを追わず、結果を `pass` / `fail` / `unknown` で区別して返します。
- **診断の途中再開**: `doctor` の実行は SQLite に記録され、`show` / `resume` / `abandon` で扱えます。設定が変わった診断は再開させず、新しく取り直します。
- **「確認済み」と「未検証」を混ぜない**: レポートに `fail` や `unknown` があっても隠さない方針を、設計文書（`docs/OPS_LANGGRAPH.md`）に明記しています。

## ディレクトリ構成

```
ops_harness/
  workflow.py          LangGraph の StateGraph（受付 → 診断 → 担当作業 → 検証 → レビュー → 完了/失敗）
  workflow_actions.py  決定的な構造検証（ハッシュ・成果物の実在など）。意味のレビューはグラフ側
  workflow_cli.py      job の CLI（list / start / status / submit / review / continue / cancel / gate / pin）
  workflow_lock.py     同一ホスト上の job ロック（OS が所有を管理し、クラッシュ時も解放される）
  verifier_pins.py     検証プログラムの版固定（写しの作成と整合確認）
  pinned_entry.py      pin の写しから実行するための入口
  development.py       開発 job（OpenSpec 成果物）向けのアダプタ
  probes.py            読み取り専用の診断
  store.py             診断 run の SQLite 保存（start / record / finish / resume / abandon）
  __main__.py          doctor / show / resume / abandon / route の CLI
scripts/ops/harness.py        共通の入口（--setup で専用 venv を作る）
config/ops_harness.json       サービス台帳の例（合成サンプル）
docs/OPS_LANGGRAPH.md         設計の考え方
tests/                        store / probes / workflow / development のテスト
```

## 動かし方

必要なもの: Python 3.10 以上、`requirements-ops.lock.txt` の依存（`langgraph`、`langgraph-checkpoint-sqlite`）。

```powershell
python scripts/ops/harness.py --setup          # .harness/venv を作り、依存を入れる
python scripts/ops/harness.py job list
python scripts/ops/harness.py job start --job-id demo-001 --request request.json
python scripts/ops/harness.py job status demo-001
python scripts/ops/harness.py doctor           # 読み取り専用の診断
```

環境変数（任意）:

| 変数 | 用途 |
|---|---|
| `OPS_APP_HEALTH_URL` | 診断が確認するアプリのヘルス URL（任意。未設定ならそのプローブは `unknown` で skip） |
| `OPS_FRONTEND_URL` | 診断が確認するフロントエンドの URL（任意。未設定ならそのプローブは `unknown` で skip） |

テスト（`pytest` が必要）:

```powershell
python -m pytest tests
```

## 制限事項（閉包）

- **同梱していない依存**（2 件）: `langsmith`（任意。無ければ何もしない tracing に切り替わる）、`pytest`（テスト用）。`langgraph` と `langgraph-checkpoint-sqlite` は `requirements-ops.lock.txt` に固定しています。
- **合成スタブ・合成サンプル**: `scripts/` 配下の Markdown 行長 lint（最小のスタブ。自前のものに差し替える前提）、`config/ops_harness.json`（サービス台帳の例）、`requirements-ops.lock.txt` の注記、`tests/test_ops_harness_probes.py` のサンプル値は合成です。実運用のサービス名・パスは含みません。
- 他の公開リポジトリとの関係: 開発 job（`software-development`）の検証は、`dev-acceptance-harness` の `gate.py` を呼び出します。両方を同じワークスペースに重ねて使う想定です。
- 設計文書が参照する運用側の方針文書・旧設計資料は同梱していません。
- 診断のうち Windows タスクの確認は Windows 前提です。外部サービスの鍵は使いません。

## ライセンス

All rights reserved. 閲覧目的で公開しています。詳細は LICENSE を参照。
