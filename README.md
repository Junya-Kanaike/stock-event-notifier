# stock-event-notifier

日本株のPO、IPO、立会外分売、CB、株式分割を検知し、イベント別のSlackチャンネルへ通知する個人向けMVPです。TDnetとJPXの公開情報だけを使い、GitHub Actions上で無人運用します。

詳細な業務仕様は [event_notifier_spec.md](event_notifier_spec.md) を参照してください。

## 主な処理

- `poll_tdnet`: 平日の日中にTDnetを巡回し、PO、立会外分売、CB、株式分割を処理
- `daily_morning`: 営業日の朝にJPXデータを更新し、当日分と未送信の遅延通知を処理
- `state/events.json`: イベント、通知済み開示ID、取得元の稼働状態を保存
- `state/cache`: JPX銘柄、信用区分、IPO、立会外分売の取得結果を保存

GitHub Actionsのscheduled workflowは実行時刻を保証しません。遅延または失敗した日次通知は、次回実行時に本来の日付を明記して再送されます。

## 必要環境

- Python 3.12
- Slack Incoming Webhook 6本（systemは任意）
- GitHub Actionsから状態をpushできるリポジトリ権限

## ローカルセットアップ

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

構文確認も含める場合:

```bash
python -m compileall -q src tests
```

## Slack設定

GitHub ActionsのRepository secretsに以下を登録します。

- `SLACK_WEBHOOK_PO`
- `SLACK_WEBHOOK_IPO`
- `SLACK_WEBHOOK_BUNBAI`
- `SLACK_WEBHOOK_CB`
- `SLACK_WEBHOOK_SPLIT`
- `SLACK_WEBHOOK_SYSTEM`（運用アラート用、任意）

Webhook URLをコード、状態JSON、ログへ記録しないでください。設定後は `test_notify` workflowを手動実行して全チャンネルへの到達を確認します。

## 手動実行

外部データを取得しますが、Slack送信と状態・キャッシュ更新を行わない確認:

```bash
python -m src.run_poll --date 2026-07-16 --dry-run
python -m src.run_daily --date 2026-07-16 --dry-run
```

本番送信を行う場合は `--dry-run` を外します。ローカルからの本番送信は、対象Webhookと日付を確認してから実行してください。

## GitHub Actions

- `.github/workflows/poll_tdnet.yml`: TDnet巡回
- `.github/workflows/daily_morning.yml`: 朝の日次通知
- `.github/workflows/test_notify.yml`: Webhook疎通確認
- `.github/workflows/ci.yml`: 単体テスト、構文確認、依存関係監査

状態変更は `scripts/commit_state.sh` がcommitし、競合時はrebaseして最大3回pushを試行します。
単体テストはコード変更時のCIへ集約し、定期通知workflowではデータ取得と通知だけを実行します。

標準cronは混雑しやすい毎時0分を避け、TDnetをJST平日08:03から19:53まで10分間隔、日次処理を07:17に設定しています。それでもGitHub Actionsはscheduled eventの実行時刻を保証しません。

厳密な10分間隔が必要な場合は、外部cronから次のREST APIを呼び出します。workflow側は `workflow_dispatch` に対応済みです。

```text
POST https://api.github.com/repos/Junya-Kanaike/stock-event-notifier/actions/workflows/poll_tdnet.yml/dispatches
Authorization: Bearer <fine-grained token>
Content-Type: application/json

{"ref":"main"}
```

トークンはActionsのwrite権限だけを付け、外部サービスのsecret機能に保存します。GitHub標準cronはフォールバックとして残してください。

## 運用確認

毎営業日に次を確認します。

1. `daily_morning` が市場開始前に完了していること
2. `poll_tdnet` の実行間隔が許容範囲内であること
3. `SLACK_WEBHOOK_SYSTEM` に取得失敗や古いキャッシュの警告がないこと
4. `state/events.json` の未送信スケジュールと実際のSlack通知が一致すること

障害時はActionsの失敗ジョブを再実行します。日次通知は次回実行でも回収されますが、遅延した売買指示は現在時点の指示として扱わず、内容を再確認してください。

## データ取得とキャッシュ

- TDnet: yanoshin APIを優先し、取得不能または空の場合はTDnet HTMLへフォールバック
- JPX信用区分・IPO・立会外分売: 日次ジョブで強制更新
- JPX銘柄マスター: 月次データのためキャッシュを利用

日次強制更新に失敗して既存キャッシュを使った場合、キャッシュ取得日時を含むsystem通知を送ります。

## 回帰テストの追加

PDF書式の変更を再現する場合、公開PDF全体ではなく解析に必要な最小限の抽出テキストを `tests/fixtures/tdnet` に保存します。fixtureには元PDFのURLを記録し、対応するテストを `tests/test_parsers.py` または `tests/test_tdnet_keywords.py` に追加してください。

## 既知の制約

- GitHub Actionsのcronは遅延・欠落する可能性があり、厳密な10分SLAは保証できません。
- PDF解析は正規表現ベースのため、未知の書式では一部項目が「取得失敗」になります。
- 本システムの通知は情報整理用であり、注文執行や投資成果を保証しません。
