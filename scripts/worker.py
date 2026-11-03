"""Run the one-instance grabber every fifteen minutes until success or a blocked state."""
import argparse
import fcntl
import grab
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent

STOP = threading.Event()
TERMINAL = {"free_quota_guard", "instance_already_exists", "unknown_launch_result", "instance_terminal"}


def stop(signum, frame):
    STOP.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--interval", type=int, default=900)
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("interval must be at least 60 seconds")
    settings_path = str(Path(args.settings).expanduser().resolve())
    grab.configure(settings_path)
    STATE = grab.STATE
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (STATE / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (STATE / "finished.json").exists():
            print(json.dumps({"status": "previous_run_finished"}), flush=True)
            return
        failures = 0
        while not STOP.is_set():
            process = subprocess.run(
                [sys.executable, str(ROOT / "grab.py"), "--settings", settings_path, "--launch"],
                capture_output=True, text=True,
            )
            try:
                result = json.loads(process.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                result = {"status": "worker_error", "exit_code": process.returncode}
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            temporary = STATE / "latest.tmp"
            temporary.write_text(json.dumps(result))
            temporary.replace(STATE / "latest.json")
            print(json.dumps(result), flush=True)
            status = result["status"]
            successful = status == "submitted" and result.get("instance_state") == "RUNNING"
            failures = failures + 1 if status in {"api_error", "error", "worker_error", "capacity_unknown"} else 0
            if successful or status in TERMINAL or failures >= 3:
                (STATE / "finished.json").write_text(json.dumps(result))
                return
            STOP.wait(30 if status == "submitted" else args.interval)


if __name__ == "__main__":
    main()
