# SNS投資アイデア監視（idea_watch）

YouTubeとXの指定チャンネル・アカウントを1日2回（JST 07:13 / 16:13）巡回し、個別銘柄の投資アイデアをClaudeで抽出してNotionの「投資アイデア」データベースに蓄積します。既存のイベント通知とは状態・実行枠を共有しません。

## 仕組み

```
Notion「監視ソース」 → X API v2（since_id）／YouTube RSS＋字幕
      → Claude API（銘柄・方向・根拠・カタリストを構造化抽出）
      → Notion「投資アイデア」へ1銘柄×1投稿で追加（ソースIDで重複防止）
      → 監視ソースのカーソル・最終取得を更新
```

- 監視リスト・カーソル・抽出結果はすべてNotionに置きます。このリポジトリはpublicで、Actionsのログも公開されるため、リポジトリへのcommitやログへの内容出力は行いません（ログは件数のみ）。
- 1ソースが失敗しても他は続行し、失敗ソースのカーソルは進めません。失敗があれば `SLACK_WEBHOOK_SYSTEM` に件数とHTTPステータスだけを通知します。
- 保有銘柄（`IDEA_HOLDINGS`）に一致したアイデアは「保有銘柄関連」にチェックが付きます。

## 事前準備

### 1. Notion

「SNS投資アイデア監視」ページの下に、以下の2つのデータベースを作成済みです。

| DB | data source ID |
|---|---|
| 監視ソース | `c6f327af-82b0-42b2-9b17-5253869121cf` |
| 投資アイデア | `ca344f15-4918-441c-b5f1-9d1b466c3459` |

1. https://www.notion.so/profile/integrations で内部インテグレーションを作成し、トークンを控える（機能: コンテンツの読み取り・更新・挿入）。
2. 「SNS投資アイデア監視」ページ右上の「…」→「接続」から、そのインテグレーションを追加する（子DBにも継承されます）。
3. 「監視ソース」に1行ずつ追加し、「有効」にチェックする。
   - X: プラットフォーム=X、ハンドル=@なしユーザー名
   - YouTube: プラットフォーム=YouTube、ハンドル=`UC`で始まるチャンネルID（チャンネルページの「概要」→「チャンネルを共有」→「チャンネルIDをコピー」）
   - 「内部ID」「カーソル」「最終取得」はシステムが書き込みます。

### 2. API

- **Anthropic**: https://console.anthropic.com でAPIキーを発行。
- **X**: https://console.x.com でアプリを作成し、Bearer Tokenを発行。従量課金なのでクレジットを購入し、使用上限を設定しておく。X未設定ならYouTubeだけで動きます。

### 3. GitHub（Settings → Secrets and variables → Actions）

Secrets:

| 名前 | 内容 |
|---|---|
| `NOTION_TOKEN` | Notionインテグレーションのトークン |
| `ANTHROPIC_API_KEY` | AnthropicのAPIキー |
| `X_BEARER_TOKEN` | XのBearer Token（任意） |
| `IDEA_HOLDINGS` | 保有銘柄。例 `285A:キオクシア,6857:アドバンテスト,6920:レーザーテック`（任意） |
| `IDEA_YT_PROXY_URL` | 字幕取得用プロキシURL（任意、後述） |

Variables:

| 名前 | 値 |
|---|---|
| `IDEA_WATCH_ENABLED` | `true`（これが無いとscheduleでも動きません） |
| `IDEA_SOURCES_DATA_SOURCE_ID` | `c6f327af-82b0-42b2-9b17-5253869121cf` |
| `IDEA_IDEAS_DATA_SOURCE_ID` | `ca344f15-4918-441c-b5f1-9d1b466c3459` |
| `IDEA_MODEL` | 任意。既定 `claude-sonnet-5-5`。安くするなら `claude-haiku-5-5` |
| `IDEA_X_MAX_POSTS_PER_RUN` | 任意。1回あたりのX読み取り上限（既定300） |

設定後、Actionsから `idea_watch` を手動実行（Run workflow）して、Notionに行が入ることを確認します。

## コストの目安

- **X**: 投稿の読み取り1件ごとに課金（2026年10月時点で約$0.005/件）。初回のユーザーID解決は1アカウント1回のみ（約$0.01）。30アカウント×1日平均5投稿なら月4,500件で約$23。上限は `IDEA_X_MAX_POSTS_PER_RUN` と、アカウントごとの `IDEA_X_MAX_POSTS_PER_SOURCE`（既定20）で制御します。リポスト・リプライは既定で除外（`IDEA_X_INCLUDE_REPLIES=1` で含める）。
- **YouTube**: RSSと字幕取得は無料。
- **Claude**: X投稿は発信者ごとに最大20件まとめて1回、動画は1本1回。実行ログの `claude_tokens` で消費を確認できます。

## YouTube字幕の制約

YouTubeはクラウド事業者のIPからの字幕取得をよく遮断し、GitHub Actions（Azure）も対象になりがちです。遮断時は概要欄だけで抽出し、Notionの本文に「抽出元: 概要欄のみ」と記録します。ログの `youtube_transcripts blocked=True` で確認できます。

字幕まで確実に読みたい場合の選択肢:

1. `IDEA_YT_PROXY_URL` に住宅用プロキシ（例: Webshareのrotating residential）のURLを設定する。
2. 自宅のMacから手動または定期実行する（自宅回線なら遮断されにくい）。

```sh
# .env などで上記の環境変数を設定したうえで
.venv/bin/python -m src.run_ideas --dry-run   # 書き込まずに抽出結果を表示
.venv/bin/python -m src.run_ideas             # Notionへ書き込み
```

`--dry-run` は抽出内容を標準出力に出すため、ローカル専用です（Actionsでは使わない）。

## 既知の制約

- 1回の巡回で1チャンネルあたり最新3本（`IDEA_YT_MAX_VIDEOS_PER_SOURCE`）まで。それより多い新着は古いものから読み飛ばします。
- 初回（カーソル未設定）は直近24時間（`IDEA_INITIAL_LOOKBACK_HOURS`）だけを対象にします。
- X APIの最小取得件数が5件のため、読み取り上限は最大4件超過することがあります。
- 抽出はAIによる要約です。発信者の主張の整理であり、売買判断の前に開示・決算などの一次情報で確認してください。
