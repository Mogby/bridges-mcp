#!/usr/bin/env python3
"""Send one delayed test to the current chat, allowing its turn to finish first."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def main():
    thread_id = os.environ.get("CODEX_THREAD_ID")
    codex = os.environ.get("CODEX_APP_RUNTIME_CLI_PATH")
    if not thread_id or not codex:
        raise SystemExit("Run from a Codex shell tool with its explicit thread and CLI environment")
    marker = "bridges-idle-" + uuid.uuid4().hex[:12]
    delay = 20
    print(json.dumps({"scheduled": True, "marker": marker, "delay_seconds": delay,
                      "thread_id": thread_id}), flush=True)
    time.sleep(delay)
    sent_at = datetime.now(timezone.utc).isoformat()
    message = (f"Local bridge idle-wake test: {marker}. Submitted at {sent_at} "
               "after a 20-second delay. Acknowledge receipt, check whether the prior "
               "agent turn had already finished before submission, and record the "
               "result of the bridge experiment. Do not schedule another test automatically.")
    result = subprocess.run([codex, "queue", "--thread", thread_id, "--message", message],
                            capture_output=True, text=True, timeout=25)
    record = {"marker": marker, "submitted_at": sent_at, "thread_id": thread_id,
              "returncode": result.returncode, "stdout": result.stdout.strip(),
              "stderr": result.stderr.strip()}
    directory = Path(__file__).resolve().parent / ".runtime"
    directory.mkdir(exist_ok=True)
    (directory / (marker + ".json")).write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record), flush=True)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
