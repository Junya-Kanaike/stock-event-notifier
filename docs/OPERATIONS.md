# 通知システムの運用と復旧

## 本番と作業場所

本番は [GitHubのmain](https://github.com/Junya-Kanaike/stock-event-notifier/tree/main) と、mainの `state/events.json` に保存された処理・送信記録で確認する。ローカルブランチや古いremote-tracking refだけでは本番反映の証拠にならない。

2026-11-04にシェルから実在を確認した主作業場所は `/Users/junya.k/_開発:dev_/stock-notifier`。`★開発:dev` や `_開発/dev_` という別表記は見つからなかった。編集前に実在パスとGitブランチを照合する。OneDriveの残存フォルダを自動で同期したり、別チェックアウトのブランチを切り替えたりしない。

```sh
pwd
git rev-parse --show-toplevel
git status --short --branch
```

## Mac補助の版と更新

配布するファイルは `scripts/mac_scheduler.py` と `scripts/monitor_health.py` の2本。共通監視モジュールが欠けた状態では起動時エラーになり、監視を黙って無効化しない。

mainへの反映後、作業フォルダのPythonから次を実行する。`update` は稼働中のtickと共通のロックを取り、既存2ファイルを日時付きでバックアップし、監視モジュール→本体の順で置き換える。次のtickから反映される。launchdの再登録、ログ・状態の削除は不要。

```sh
python scripts/mac_scheduler.py check
python scripts/mac_scheduler.py update
python scripts/mac_scheduler.py status
launchctl print "gui/$(id -u)/jp.stock-event-notifier.watchdog"
```

`~/Library/Application Support/stock-event-notifier/installed-version.json` に配布日時と2ファイルのSHA-256を保存する。登録だけで正常と判断せず、更新後の `watchdog-state.json` の確認日時・`github_health`・警告も確認する。

GitHub確認失敗は、秘密情報を含まないAPIの用途・例外種別・HTTPステータス・連続失敗回数を残す。1〜2回の一時エラーでは警告通知せず、3回連続で警告する。読み取り専用checkは1回の失敗でも終了コード1を返す。復旧時刻と直前の失敗回数は記録に残す。起動要求の受付が不明なときは従来どおり10分以内に繰り返さない。

## 予定通知の送信記録と復元

予定通知には「イベントID・予定日・ラベル」から作る固定の `notification_id` を付ける。送信前の `started` と送信後の `accepted` / `failed` / `unknown` を、Git管理のstateとは別のJSON Linesファイルに逐次保存・fsyncする。

本番3ワークフローは、送信前に過去の `notifier-delivery-receipts` 成果物を確認する。mainでscheduleまたはworkflow_dispatchから動いた本番ワークフローで、同一リポジトリ由来の記録だけを受け入れる。イベント・日付・ラベルが一致した受付成功を復元するので、送信後のstate push失敗による再送を防げる。予定日変更に送信済みを引き継がない。

送信結果が不明、または `started` のまま終了した記録は `sent=true` にしない。`delivery_hold` として自動再送を止め、Mac・クラウドの監視と日次集約に出す。Slackの読み取りタイムアウト、接続切断、HTTP 5xxでは、Slackが既に受け付けた可能性があるため自動でHTTP再送しない。接続開始時のタイムアウトとHTTP 429は、最大3回の範囲で安全に再試行する。429ではRetry-Afterを尊重し、待ち時間が30秒超なら後続ワークフローへ回す。

成果物の読み取りが失敗した場合、照合せずに予定通知を重ねず、ジョブを失敗として次回へ回す。成果物は90日保持し、HTTP応答不明の通知と、通知後のstate保存失敗を調査できるようにする。

制約として、stateの保存と成果物の保存の両方が失敗する障害や、ランナー自体の喪失に対して完全な重複排除は保証しない。この記録復元は予定通知が対象であり、発表検知やsystem通知を含むすべての送信にexactly-onceを提供するものではない。

## 送信結果不明の確認

まず該当Slackチャンネルで銘柄、予定日、通知ラベル、送信試行時刻を照合する。照合できるまで保留を解除しない。mainの最新stateを持つ専用作業ツリーで、対象のnotification_idを指定して結果をプレビューする。

```sh
python -m scripts.delivery_receipts --resolve NOTIFICATION_ID --confirmed sent --reason 'Slackの該当メッセージを確認'
python -m scripts.delivery_receipts --resolve NOTIFICATION_ID --confirmed sent --reason 'Slackの該当メッセージを確認' --apply
```

確実に未着と確認した場合だけ `--confirmed not-sent` を使う。`--apply` はローカルstateへの記録であり、本番反映には最新mainと競合を確認した上でstateを反映する。解除は送信操作を行わず、次の既存ジョブが現在の時刻ゲートに従って回収する。保存済みの送信確認・未着確認は、それ以前の成果物を読み直しても覆さない。

## 過去POの保留

発表から90日以上、既知の価格決定・受渡日程から30日超が経過し、通知予定がなく、元資料の回復失敗が記録された判定待ちPOは `historical_review` に理由と参照URLを保存する。元のeligibilityや未取得値は保持する。対象外・送信済みとして処理しない。

自動で過去PDFや株価を取り直す処理を停止し、日次集約では現在の判定待ちとは別の「過去POの履歴保留」に表示する。新資料が見つかって数値を確定できても、保留を明示的に見直すまで過去の売買指示を発火させない。

```sh
python -m scripts.repair_reliability --as-of 2026-11-04
```

このプレビューは送信・保存を行わない。今回の本番stateでは543A・4071・3282・205Aの4件だけが履歴保留になることを検証する。
