"""Restore scheduled-notice receipts from trusted main workflow artifacts."""
from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import time
from zipfile import ZipFile

from src.core.delivery import apply_receipts, resolve_delivery_hold
from src.core.bizday import now_jst
from src.core.store import STATE_PATH, load_state, save_state


ARTIFACT_NAME = "notifier-delivery-receipts"
WORKFLOWS = {"daily_morning", "timed_notifications", "poll_tdnet"}
MAX_BYTES = 2_000_000


def github_get(repo, suffix, *, binary=False):
    for attempt in range(3):
        try:
            result = subprocess.run(["gh", "api", f"repos/{repo}/{suffix}"],
                                    capture_output=True, check=True, timeout=30)
            return result.stdout if binary else json.loads(result.stdout)
        except (subprocess.SubprocessError, OSError, ValueError):
            if attempt < 2:
                time.sleep(2**attempt)
    # Do not echo stderr or exception chains, which can contain credentials.
    raise RuntimeError("Could not restore delivery receipts from GitHub; sending is stopped") from None


def decode_receipts(data):
    if len(data) > MAX_BYTES:
        raise ValueError("Receipt archive is too large")
    with ZipFile(BytesIO(data)) as archive:
        files = archive.infolist()
        if len(files) != 1 or files[0].filename != "receipts.jsonl" or files[0].file_size > MAX_BYTES:
            raise ValueError("Unexpected receipt archive contents")
        # Read the exact member; never extract archive paths onto the filesystem.
        return [json.loads(line) for line in archive.read(files[0]).decode("utf-8").splitlines() if line.strip()]


def restore(state, repo, *, get=github_get):
    applied = list(state.get("delivery_receipt_artifacts", []))
    known = set(applied)
    artifacts = []
    for page in range(1, 21):
        payload = get(repo, f"actions/artifacts?per_page=100&page={page}")
        batch = payload["artifacts"]
        artifacts.extend(a for a in batch if (a.get("name") == ARTIFACT_NAME or
                         re.fullmatch(ARTIFACT_NAME + r"-[0-9]+-[0-9]+", a.get("name", "")))
                         and not a.get("expired") and a["id"] not in known)
        if len(batch) < 100:
            break
    else:
        raise RuntimeError("Receipt history exceeds the restore limit; review before sending")
    count = 0
    # Oldest first. A later accepted result can resolve an earlier uncertain one.
    for artifact in sorted(artifacts, key=lambda a: a["id"]):
        run_id = artifact.get("workflow_run", {}).get("id")
        if not run_id:
            raise ValueError("Receipt artifact has no originating workflow run")
        if artifact["name"] != ARTIFACT_NAME and artifact["name"].split("-")[-2] != str(run_id):
            raise ValueError("Receipt artifact name does not match its originating run")
        run = get(repo, f"actions/runs/{run_id}")
        if (run.get("head_branch") != "main" or run.get("event") not in {"schedule", "workflow_dispatch"}
                or run.get("name") not in WORKFLOWS or run.get("head_repository", {}).get("full_name") != repo):
            # Fork and PR artifacts must never suppress production notices.
            continue
        records = decode_receipts(get(repo, f"actions/artifacts/{artifact['id']}/zip", binary=True))
        count += apply_receipts(state, records, str(run_id))
        applied.append(artifact["id"])
    if applied != list(state.get("delivery_receipt_artifacts", [])):
        state["delivery_receipt_artifacts"] = applied[-2000:]
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "Junya-Kanaike/stock-event-notifier"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--resolve", metavar="NOTIFICATION_ID")
    parser.add_argument("--confirmed", choices=("sent", "not-sent"))
    parser.add_argument("--reason")
    parser.add_argument("--apply", action="store_true", help="Persist an explicitly reviewed held result")
    args = parser.parse_args(argv)
    state = load_state(args.state)
    before = json.dumps(state, sort_keys=True)
    if args.resolve:
        if not args.confirmed or not args.reason or args.dry_run and args.apply:
            parser.error("--resolve requires --confirmed and --reason; --dry-run and --apply cannot be combined")
        resolve_delivery_hold(state, args.resolve, args.confirmed, args.reason, now_jst())
        if args.apply:
            save_state(state, args.state)
        print("Reviewed delivery result " + ("saved" if args.apply else "previewed (use --apply to save)"))
        return 0
    if args.confirmed or args.reason or args.apply:
        parser.error("Review options require --resolve")
    count = restore(state, args.repo)
    if not args.dry_run and json.dumps(state, sort_keys=True) != before:
        save_state(state, args.state)
    print(f"Delivery receipts checked: {count} schedules restored or held" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
