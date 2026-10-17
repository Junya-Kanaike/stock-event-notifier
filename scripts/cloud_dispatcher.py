"""Keep dispatching main jobs between unreliable cron triggers.

Default mode checks once without writing or sending. --live is restricted to
the opted-in public repository's main GitHub Actions workflow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
import shutil
import subprocess
import time

from scripts.mac_scheduler import JST, REPOSITORY, gh_json, inspect_remote, parse_time
from src.notifiers.slack import SlackNotifier


WORKFLOW = "continuous_dispatcher.yml"
RUN_SECONDS = 165 * 60


def request_workflow(gh, repo, workflow):
    subprocess.run([gh, "workflow", "run", workflow, "--repo", repo, "--ref", "main"],
                   check=True, capture_output=True, text=True, timeout=30)


def dispatch_cycle(gh, repo, current, local, *, live=False, notifier=None):
    try:
        plan = inspect_remote(gh, repo, current, local)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        plan = {"dispatch": None, "warnings": [f"GitHub確認失敗 ({type(exc).__name__})"]}
    name = plan.get("dispatch")
    if live and name:
        # Reserve before POST, even if the response is lost.
        local.setdefault("dispatches", {})[name] = current.isoformat()
        try:
            request_workflow(gh, repo, name + ".yml")
            plan["request_accepted"] = name
        except (subprocess.SubprocessError, OSError) as exc:
            plan["warnings"].append(f"{name}: 起動要求失敗 ({type(exc).__name__})")
    warnings = plan.get("warnings", [])
    previous = parse_time(local.get("last_alert_at"))
    if live and notifier and warnings and (previous is None or current - previous >= timedelta(hours=1)):
        try:
            notifier.system("[起動・通知時刻の監視]\n" + "\n".join(warnings[:10]))
            local["last_alert_at"] = current.isoformat()
        except Exception as exc:
            plan["warnings"].append(f"監視アラート送信失敗 ({type(exc).__name__})")
    return plan


def request_successor(gh, repo, run_id):
    payload = gh_json(gh, f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?branch=main&per_page=10")
    if any(str(r.get("id")) != str(run_id) and r.get("status") != "completed"
           and r.get("head_branch") == "main" for r in payload["workflow_runs"]):
        return
    request_workflow(gh, repo, WORKFLOW)


def validate_live_environment(gh, repo):
    if (repo != REPOSITORY or os.getenv("GITHUB_ACTIONS") != "true"
            or os.getenv("GITHUB_REF") != "refs/heads/main"
            or os.getenv("NOTIFIER_CONTINUOUS_DISPATCHER") != "true"):
        raise RuntimeError("Live dispatcher requires an explicit opt-in on main in GitHub Actions")
    metadata = gh_json(gh, f"repos/{repo}")
    if metadata.get("private") is not False:
        raise RuntimeError("Continuous hosted dispatch is limited to this public repository")


def run_loop(gh, repo, *, seconds=RUN_SECONDS, monotonic=time.monotonic,
             sleep=time.sleep, now=lambda: datetime.now(JST), notifier=None):
    local = {}
    deadline = monotonic() + seconds
    last_summary = None
    while monotonic() < deadline:
        plan = dispatch_cycle(gh, repo, now(), local, live=True, notifier=notifier)
        summary = {k: plan.get(k) for k in ("request_accepted", "warnings")}
        if summary != last_summary:
            print(json.dumps({"checked_at": now().isoformat(), **summary}, ensure_ascii=False), flush=True)
            last_summary = summary
        remaining = deadline - monotonic()
        if remaining <= 60:
            break
        sleep(min(60, remaining))
    # Self-handoff keeps continuity without waiting for the next cron. The
    # workflow has its own concurrency group, separate from state-writing jobs.
    for attempt in range(3):
        try:
            request_successor(gh, repo, os.getenv("GITHUB_RUN_ID"))
            print("Successor accepted or already queued", flush=True)
            return 0
        except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
            print(f"Successor request failed ({type(exc).__name__})", flush=True)
            if attempt < 2:
                sleep(10)
    raise RuntimeError("Dispatcher handoff failed; fallback cron remains enabled")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--gh", default=shutil.which("gh"))
    parser.add_argument("--repo", default=REPOSITORY)
    args = parser.parse_args(argv)
    if not args.gh:
        parser.error("GitHub CLI is required")
    if not args.live:
        plan = dispatch_cycle(args.gh, args.repo, datetime.now(JST), {})
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 1 if plan.get("warnings") else 0
    validate_live_environment(args.gh, args.repo)
    return run_loop(args.gh, args.repo, notifier=SlackNotifier(dry_run=False))


if __name__ == "__main__":
    raise SystemExit(main())
