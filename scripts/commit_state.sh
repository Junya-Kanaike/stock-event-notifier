#!/usr/bin/env bash
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add state

if git diff --cached --quiet; then
  exit 0
fi

git commit -m "Update event state"
for attempt in 1 2 3; do
  if git push; then
    exit 0
  fi
  if [[ "$attempt" -eq 3 ]]; then
    break
  fi
  if ! git pull --rebase origin "${GITHUB_REF_NAME:-main}"; then
    git rebase --abort || true
    break
  fi
done

echo "Failed to persist event state after retries" >&2
exit 1
