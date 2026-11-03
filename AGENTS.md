# 通知システムの作業場所

- このPCでシェルから実在を確認した主作業場所は `/Users/junya.k/_開発:dev_/stock-notifier`。
- パスの表記に差がある場合は `pwd` と `git rev-parse --show-toplevel` で照合し、推測でコピーや移動をしない。
- 編集前に作業ディレクトリ、現在のGitブランチ、未コミット変更を確認する。
- 主作業場所のブランチを本番とみなさない。本番はGitHubの `Junya-Kanaike/stock-event-notifier` のmainと実行記録で確認する。
- ユーザーが別の作業場所を明示した場合はその指定を優先する。
- 既存チェックアウト間のコピー・同期、既存チェックアウトのブランチ切替は自動で行わない。最新mainからの修正が必要なら別の作業ツリーを作る。
- 送信を伴わない検証は `python -m unittest discover -s tests -v` と `python -m scripts.notification_matrix`。本番stateやキャッシュの変更をテスト結果に混ぜない。
- 配布用Mac補助は `scripts/mac_scheduler.py` と `scripts/monitor_health.py` の両方が必要。更新・稼働確認は `docs/OPERATIONS.md` と `docs/MAC_SCHEDULER.md` を参照する。
