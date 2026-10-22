"""Mac launchd watchdog. No Slack secrets; dispatch uses the existing gh login.

check is read-only. tick dispatches at most one workflow. install is explicit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo
import jpholiday


def health_warnings(state, now):
    # The installed Mac helper can run without the source checkout. Keep its
    # original standalone operation; the cloud controller has the full module.
    try:
        from src.core.operations import stale_sources, late_delivery_warnings
    except ImportError:
        return []
    return stale_sources(state, now) + late_delivery_warnings(state, now)

JST = ZoneInfo("Asia/Tokyo")
REPOSITORY = "Junya-Kanaike/stock-event-notifier"
LABEL = "jp.stock-event-notifier.watchdog"
WORKFLOWS = ("daily_morning", "timed_notifications", "poll_tdnet")
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
        active = [r for r in runs.get(name, []) if r.get("status") != "completed"]
        busy.extend(active)
        for run in active:
            created = parse_time(run.get("created_at"))
            if created and now - created > timedelta(minutes=20):
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


def gh_json(gh, endpoint, *, raw=False):
    # The contents API omits base64 content for files over 1 MB; the raw media
    # type returns the file body itself up to 100 MB.
    headers = ["-H", "Accept: application/vnd.github.raw+json"] if raw else []
    completed = subprocess.run([gh, "api", *headers, endpoint], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(completed.stdout)


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


def save_local(data, path):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def check(gh, repo, *, live=False, support=SUPPORT):
    now = datetime.now(JST)
    state_path = support / "watchdog-state.json"
    local = json.loads(state_path.read_text()) if state_path.exists() else {}
    try:
        plan = inspect_remote(gh, repo, now, local)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        # Never log gh stderr: authentication errors can contain sensitive data.
        plan = {"dispatch": None, "warnings": [f"GitHub確認失敗 ({type(exc).__name__})"]}
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
    return 1 if plan["warnings"] else 0


def launchd_config(python, script, gh, repo, support):
    return {"Label": LABEL,
            "ProgramArguments": [str(python), str(script), "tick", "--gh", str(gh), "--repo", repo],
            "StartInterval": 60, "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(support / "watchdog.log"),
            "StandardErrorPath": str(support / "watchdog-error.log")}


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
    shutil.copy2(Path(__file__).resolve(), script)
    config = launchd_config(sys.executable, script, gh, repo, SUPPORT)
    with PLIST.open("xb") as stream:
        plistlib.dump(config, stream)
    print(f"Prepared: {PLIST}")
    if start:
        subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
        print("Started. This only works while the Mac is awake, logged in and online.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "tick", "install", "status"), nargs="?", default="check")
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
