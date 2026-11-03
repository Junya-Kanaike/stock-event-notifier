"""Mac launchd watchdog. No Slack secrets; dispatch uses the existing gh login.

check is read-only. tick dispatches at most one workflow. install is explicit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo
import jpholiday


try:
    from scripts.monitor_health import stale_sources, late_delivery_warnings
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.monitor_health"}:
        raise
    from monitor_health import stale_sources, late_delivery_warnings


def health_warnings(state, now):
    return stale_sources(state, now) + late_delivery_warnings(state, now)

JST = ZoneInfo("Asia/Tokyo")
REPOSITORY = "Junya-Kanaike/stock-event-notifier"
LABEL = "jp.stock-event-notifier.watchdog"
WORKFLOWS = ("daily_morning", "timed_notifications", "poll_tdnet")
STALE_RUN = timedelta(minutes=60)
READ_FAILURE_ALERT_AFTER = 3
SUPPORT = Path.home() / "Library/Application Support/stock-event-notifier"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(JST)


def _at(now, hour, minute=0):
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _notice_gate(clock):
    """Gate hour of the time-gated notice window containing clock, if any."""
    if time(6) <= clock < time(9, 10) or time(12) <= clock < time(12, 25) or time(19) <= clock < time(19, 25):
        return 19 if clock >= time(19) else 12 if clock >= time(12) else 8 if clock >= time(8) else 6
    return None


def freshness_requirement(name, now):
    clock = now.time().replace(tzinfo=None)
    if name == "daily_morning":
        return _at(now, 5, 30) if clock >= time(5, 40) else None
    if now.weekday() >= 5 or jpholiday.is_holiday(now.date()) or (now.month, now.day) in {(12, 31), (1, 1), (1, 2), (1, 3)}:
        return None
    if name == "poll_tdnet" and time(8) <= clock < time(20):
        return now - timedelta(minutes=10)
    if name == "timed_notifications":
        gate = _notice_gate(clock)
        if gate is not None:
            return max(now - timedelta(minutes=5), _at(now, gate))
        if time(20, 10) <= clock < time(20, 45):
            return now - timedelta(minutes=10)
        if time(9, 10) <= clock < time(20):
            return now - timedelta(minutes=30)
    return None


def overdue_since(name, now, saved_success):
    """When a due job should have saved a success: its expected time, or the
    end of the allowed gap after the last saved success. Lateness for alerts
    is measured from here, not from the age of the last success."""
    clock = now.time().replace(tzinfo=None)
    gate = _notice_gate(clock)
    if name == "daily_morning":
        return _at(now, 5, 30)
    if name == "poll_tdnet":
        start, gap = _at(now, 8), timedelta(minutes=10)
    elif gate is not None:
        start, gap = _at(now, gate), timedelta(minutes=5)
    elif clock >= time(20):
        start, gap = _at(now, 20), timedelta(minutes=10)
    else:
        start, gap = _at(now, 9, 10), timedelta(minutes=30)
    return start if saved_success is None else max(start, saved_success + gap)


def plan_dispatch(now, runs, health, local):
    warnings = []
    due = []
    if not health:
        return {"dispatch": None, "warnings": ["本番にworkflow_healthがありません。先に改善コードのmain反映と実行を確認してください。"]}
    busy = []
    for name in WORKFLOWS:
        for run in runs.get(name, []):
            if run.get("status") == "completed":
                continue
            created = parse_time(run.get("created_at"))
            age = now - created if created else timedelta(0)
            if age > STALE_RUN:
                # A run left queued for 39 hours by a GitHub incident
                # (2026-10-22 to 10-24) blocked every dispatch. Jobs time out
                # after 15 minutes, so a run this old is not holding the slot.
                # GitHub could not cancel or delete that run, so report each
                # run id once instead of on every check.
                reported = local.setdefault("stale_runs_reported", [])
                if run.get("id") not in reported:
                    reported.append(run.get("id"))
                    del reported[:-50]
                    warnings.append(f"{name}: GitHub上で{int(age.total_seconds() // 60)}分止まっている実行を無視して判定"
                                    f" (run {run.get('id')})。Actions画面でキャンセルしてください")
                continue
            busy.append(run)
            if age > timedelta(minutes=20):
                warnings.append(f"{name}: GitHub上で20分以上待機・実行中 (run {run.get('id')})")
        required = freshness_requirement(name, now)
        if required is None:
            continue
        # Saved processing evidence is authoritative. The run list is eventually
        # consistent and came back incomplete during GitHub incidents, so it is
        # used only to find active runs.
        saved_success = parse_time(health.get(name, {}).get("last_success_at"))
        if saved_success is not None and required <= saved_success <= now:
            continue
        previous_request = parse_time(local.get("dispatches", {}).get(name))
        if previous_request and now - previous_request < timedelta(minutes=10):
            continue
        due.append(name)
        late = now - overdue_since(name, now, saved_success)
        if late >= timedelta(minutes=30):
            warnings.append(f"{name}: 予定時刻から{int(late.total_seconds() // 60)}分経っても処理成功が保存されていません")
    # All production jobs share a concurrency group. Never replace its pending
    # workflow by submitting another watchdog request.
    return {"dispatch": due[0] if due and not busy else None, "warnings": warnings,
            "due": due, "active_runs": [r.get("id") for r in busy]}


class GitHubReadError(subprocess.SubprocessError):
    """Sanitized API diagnostics, never CLI stderr, URLs or credentials."""

    def __init__(self, operation, kind, http_status=None):
        self.details = {"operation": operation, "kind": kind, "http_status": http_status}
        super().__init__(f"{operation}: {kind}" + (f" HTTP {http_status}" if http_status else ""))


def gh_json(gh, endpoint, *, raw=False):
    # The contents API omits base64 content for files over 1 MB; the raw media
    # type returns the file body itself up to 100 MB.
    headers = ["-H", "Accept: application/vnd.github.raw+json"] if raw else []
    operation = "github_api"
    if "/contents/state/events.json" in endpoint:
        operation = "state"
    else:
        match = re.search(r"/actions/workflows/(daily_morning|poll_tdnet|timed_notifications|continuous_dispatcher)\.yml/runs", endpoint)
        if match:
            operation = "workflow_runs:" + match.group(1)
    try:
        completed = subprocess.run([gh, "api", *headers, endpoint], check=True, capture_output=True, text=True, timeout=30)
        return json.loads(completed.stdout)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # Extract only the status code from gh's diagnostics. Never retain the
        # rest: authentication and connection errors may contain secret values.
        match = re.search(r"\(HTTP ([1-5][0-9]{2})\)", str(getattr(exc, "stderr", "")))
        raise GitHubReadError(operation, type(exc).__name__, int(match.group(1)) if match else None) from None


def inspect_remote(gh, repo, now, local):
    runs = {}
    for name in WORKFLOWS:
        payload = gh_json(gh, f"repos/{repo}/actions/workflows/{name}.yml/runs?branch=main&per_page=30")
        runs[name] = [r for r in payload["workflow_runs"]
                      if r.get("head_branch") == "main"
                      and r.get("event") in {"schedule", "workflow_dispatch"}]
    state = gh_json(gh, f"repos/{repo}/contents/state/events.json?ref=main", raw=True)
    plan = plan_dispatch(now, runs, state.get("workflow_health", {}), local)
    failed_dates = [day for day, data in state.get("source_health", {}).get("tdnet", {}).get("by_date", {}).items()
                    if data.get("status") != "success"]
    if failed_dates:
        plan["warnings"].append("TDnet取得未完了日: " + ", ".join(sorted(failed_dates)))
    plan["warnings"].extend(health_warnings(state, now))
    return plan


def inspect_safely(gh, repo, now, local, *, inspector=None):
    previous = local.get("github_health", {})
    try:
        plan = (inspector or inspect_remote)(gh, repo, now, local)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        count = int(previous.get("consecutive_failures", 0)) + 1
        details = getattr(exc, "details", {"operation": "remote_inspection", "kind": type(exc).__name__, "http_status": None})
        health = {**previous, **details, "status": "failed", "consecutive_failures": count,
                  "first_failed_at": previous.get("first_failed_at") or now.isoformat(),
                  "last_failed_at": now.isoformat()}
        local["github_health"] = health
        label = f"GitHub確認失敗 {details['operation']} ({details['kind']}"
        if details.get("http_status"):
            label += f", HTTP {details['http_status']}"
        label += f") 連続{count}回"
        return {"dispatch": None, "warnings": [label] if count >= READ_FAILURE_ALERT_AFTER else [],
                "github_check": health.copy()}
    health = {"status": "success", "consecutive_failures": 0, "last_success_at": now.isoformat()}
    if previous.get("consecutive_failures"):
        health["recovered_at"] = now.isoformat()
        health["previous_failures"] = previous["consecutive_failures"]
        health["last_failed_at"] = previous.get("last_failed_at")
    elif previous.get("recovered_at"):
        health.update({key: previous[key] for key in ("recovered_at", "previous_failures", "last_failed_at") if key in previous})
    local["github_health"] = health
    plan["github_check"] = health.copy()
    return plan


def save_local(data, path):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def check(gh, repo, *, live=False, support=SUPPORT):
    now = datetime.now(JST)
    state_path = support / "watchdog-state.json"
    local = json.loads(state_path.read_text()) if state_path.exists() else {}
    plan = inspect_safely(gh, repo, now, local)
    if live:
        name = plan.get("dispatch")
        if name:
            # Reserve before dispatch: uncertain failures must not flood Actions.
            local.setdefault("dispatches", {})[name] = now.isoformat()
            save_local(local, state_path)
            try:
                subprocess.run([gh, "workflow", "run", name + ".yml", "--repo", repo, "--ref", "main"],
                               check=True, capture_output=True, text=True, timeout=30)
                plan["request_accepted"] = name
            except (subprocess.SubprocessError, OSError) as exc:
                plan["warnings"].append(f"起動要求失敗 ({type(exc).__name__})")
        local["last_check_at"] = now.isoformat()
        local["last_plan"] = plan
        previous_alert = parse_time(local.get("last_alert_at"))
        if plan["warnings"] and (previous_alert is None or now - previous_alert >= timedelta(minutes=30)):
            message = "株式通知の起動補助に確認事項があります。watchdog-state.jsonを確認してください。"
            try:
                result = subprocess.run(["/usr/bin/osascript", "-e",
                                         'display notification "' + message + '" with title "株式通知の稼働確認"'],
                                        capture_output=True, timeout=10)
                if result.returncode == 0:
                    local["last_alert_at"] = now.isoformat()
            except (subprocess.SubprocessError, OSError):
                plan["warnings"].append("Mac通知を表示できませんでした。ローカル記録を確認してください。")
        save_local(local, state_path)
    print(json.dumps({"checked_at": now.isoformat(), "mode": "live" if live else "read-only", **plan},
                     ensure_ascii=False, indent=2))
    return 1 if plan["warnings"] or plan.get("github_check", {}).get("status") == "failed" else 0


def launchd_config(python, script, gh, repo, support):
    return {"Label": LABEL,
            "ProgramArguments": [str(python), str(script), "tick", "--gh", str(gh), "--repo", repo],
            "StartInterval": 60, "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(support / "watchdog.log"),
            "StandardErrorPath": str(support / "watchdog-error.log")}


def deploy_helper(support, *, update=False):
    """Publish the health module first, then the entrypoint, under tick's lock."""
    script = support / "mac_scheduler.py"
    if script.exists() and not update:
        raise RuntimeError(f"Existing helper retained: {script}")
    support.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S-%f")
    files = {}
    with (support / "watchdog.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for name in ("monitor_health.py", "mac_scheduler.py"):
            source = Path(__file__).resolve().with_name(name)
            target = support / name
            if target.exists():
                shutil.copy2(target, support / (name + "." + stamp + ".bak"))
            temporary = target.with_suffix(".new")
            shutil.copy2(source, temporary)
            temporary.replace(target)
            files[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        save_local({"installed_at": datetime.now(JST).isoformat(), "sha256": files},
                   support / "installed-version.json")


def update_helper(gh, repo):
    if sys.platform != "darwin":
        raise RuntimeError("This updater is for macOS")
    if not PLIST.exists():
        raise RuntimeError("No registered helper; use install instead")
    with PLIST.open("rb") as stream:
        config = plistlib.load(stream)
    arguments = config.get("ProgramArguments", [])
    if len(arguments) < 2 or arguments[1] != str(SUPPORT / "mac_scheduler.py"):
        raise RuntimeError("Registered helper has a different path; retained without changes")
    plan = inspect_remote(gh, repo, datetime.now(JST), {})
    if any("workflow_healthがありません" in warning for warning in plan["warnings"]):
        raise RuntimeError(plan["warnings"][0])
    deploy_helper(SUPPORT, update=True)
    print("Updated helper and shared health checks. The next tick loads them; logs and state retained.")


def install(gh, repo, *, start=False):
    if sys.platform != "darwin":
        raise RuntimeError("This installer is for macOS")
    if PLIST.exists():
        raise RuntimeError(f"Existing agent retained: {PLIST}. Unload/review it before replacing.")
    # Validate login/repository and upgraded production state before installing.
    now = datetime.now(JST)
    plan = inspect_remote(gh, repo, now, {})
    if any("workflow_healthがありません" in warning for warning in plan["warnings"]):
        raise RuntimeError(plan["warnings"][0])
    SUPPORT.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    script = SUPPORT / "mac_scheduler.py"
    if script.exists():
        raise RuntimeError(f"Existing helper retained: {script}")
    deploy_helper(SUPPORT)
    config = launchd_config(sys.executable, script, gh, repo, SUPPORT)
    with PLIST.open("xb") as stream:
        plistlib.dump(config, stream)
    print(f"Prepared: {PLIST}")
    if start:
        subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
        print("Started. This only works while the Mac is awake, logged in and online.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "tick", "install", "update", "status"), nargs="?", default="check")
    parser.add_argument("--gh", default=shutil.which("gh"))
    parser.add_argument("--repo", default=REPOSITORY)
    parser.add_argument("--start", action="store_true", help="Start launchd after explicit installation")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(f"Agent file: {PLIST} (exists={PLIST.exists()})")
        path = SUPPORT / "watchdog-state.json"
        print(path.read_text() if path.exists() else "No local execution record")
        return 0
    if not args.gh:
        parser.error("GitHub CLI (gh) is required; run gh auth login first.")
    if args.command == "install":
        install(args.gh, args.repo, start=args.start)
        return 0
    if args.command == "update":
        update_helper(args.gh, args.repo)
        return 0
    if args.command == "tick":
        SUPPORT.mkdir(parents=True, exist_ok=True)
        with (SUPPORT / "watchdog.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            # Bound both launchd output logs to 2 MB with one backup.
            for name in ("watchdog.log", "watchdog-error.log"):
                path = SUPPORT / name
                if path.exists() and path.stat().st_size > 2_000_000:
                    shutil.copy2(path, path.with_suffix(".previous.log"))
                    path.write_text("")
            return check(args.gh, args.repo, live=True)
    return check(args.gh, args.repo)


if __name__ == "__main__":
    raise SystemExit(main())
