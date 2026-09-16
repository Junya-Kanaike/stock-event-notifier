# このMacで定期起動を補強する

## 現在の状態

2026-09-17時点ではスクリプト・テストを用意済みです。LaunchAgentの登録・起動、GitHubへの起動要求、Slack実送信はまだ行っていません。

GitHub側の既存cronを残し、このMacが起きている間だけ不足した起動を補います。Macのスリープ・終了・ログアウト・ネット断の間は補助できません。GitHubの受付・ランナーの待ち時間もあるため、指定時刻の到着を保証する仕組みではありません。

## 実装

- `scripts/mac_scheduler.py`: 60秒ごとの確認用。既存のGitHub CLI認証を使用し、Slack Webhookやアクセストークンは設定ファイルに保存しません。
- `check`: 読み取り専用。GitHubの実行履歴とmainのstate内の処理成功記録を照合します。
- `tick`: 不足時に1回あたり最大1ワークフローをmainへ起動要求します。
- `install --start`: 明示的に実行した場合だけ、このMacへ登録して開始します。

全ワークフローがstate更新用の同じ実行枠を使うため、どれかが待機・実行中なら追加起動しません。受け付け状況が不明でも同じ要求を10分以内には繰り返しません。20分以上の待機・実行、保存済み処理記録の不足、TDnet取得未完了日を警告します。Mac通知は同じ状況で30分以内には繰り返しません。

| 対象 | 補助対象の時間（JST） |
|---|---|
| 朝同期 | 毎日05:40以降、その日の成功が確認できるまで |
| TDnet | 東証営業日08:00～20:00、10分以上成功記録が古い場合 |
| 予定通知 | 06:00～09:10、12:00～12:25、19:00～19:25で5分以上古い場合 |
| 日中の取りこぼし回収 | 09:10～20:00、30分以上古い場合 |
| 集約 | 20:10～20:45、10分以上古い場合 |

08:00、12:00、19:00の直前に成功したジョブは、時刻到来後の通知を送った証拠とは扱いません。祝日・年末年始も除外します。

## 有効化の順番

1. 今回の変更を承認のうえpushし、PRのCIを確認してmainへ反映する。
2. 改善後のワークフローを実行し、mainの`state/events.json`に`workflow_health`が保存されたことを確認する。古い本番のままではインストーラーは登録を拒否する。
3. 作業フォルダーから、読み取り専用チェックを行う。

```sh
.venv/bin/python scripts/mac_scheduler.py check
```

4. 結果を確認後、常駐化の承認を得て登録する。

```sh
.venv/bin/python scripts/mac_scheduler.py install --start
```

インストーラーは実行時のPythonとghの絶対パスを記録します。現在のPython環境には`requirements.txt`の依存（特にjpholiday）が必要です。Mac通知の表示許可が必要な場合があります。スリープや省電力の設定は自動で変更しません。

## 保存先・確認

- `~/Library/LaunchAgents/jp.stock-event-notifier.watchdog.plist`
- `~/Library/Application Support/stock-event-notifier/mac_scheduler.py`
- 同フォルダーの`watchdog-state.json`: 最終確認時刻・要求受付・警告
- 同フォルダーの`watchdog.log`、`watchdog-error.log`: 各2 MBでローテーション、前回分1個を保持

```sh
.venv/bin/python scripts/mac_scheduler.py status
launchctl print "gui/$(id -u)/jp.stock-event-notifier.watchdog"
```

statusのファイル存在だけでは稼働確認になりません。launchctlの登録、最終確認時刻、GitHub上のworkflow_dispatch実行、mainの保存済み処理記録の更新を全て確認してください。

## 停止・更新

停止のみ（ファイルは残す）:

```sh
launchctl bootout "gui/$(id -u)/jp.stock-event-notifier.watchdog"
```

インストーラーは既存の登録ファイルや補助スクリプトを上書きしません。更新時は停止後、既存2ファイルを退避してから再登録してください。ログ・記録は保持します。別チェックアウトへのコピーやブランチ切替は不要です。
