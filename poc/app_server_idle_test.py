#!/usr/bin/env python3
"""One-shot idle turn/steer probe against an isolated, ephemeral App Server chat.

No login credentials, model requests, or persistent chats are needed. This tests
idle steering validation only, not subscription authentication or active steering.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bridges-idle-app-server-") as tmp:
        env = dict(os.environ, CODEX_HOME=tmp)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(key, None)
        with open(Path(tmp) / "stderr.log", "w") as stderr:
            proc = subprocess.Popen([args.codex, "app-server", "--stdio"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=stderr, text=True, env=env)
            messages = queue.Queue()

            def reader():
                for line in proc.stdout:
                    messages.put(json.loads(line))
                messages.put(None)

            threading.Thread(target=reader, daemon=True).start()
            counter = 0

            def request(method, params):
                nonlocal counter
                counter += 1
                proc.stdin.write(json.dumps({"id": counter, "method": method,
                                             "params": params}) + "\n")
                proc.stdin.flush()
                while True:
                    message = messages.get(timeout=30)
                    if message is None:
                        raise RuntimeError("App Server exited before replying")
                    if message.get("id") == counter:
                        return message

            try:
                init = request("initialize", {"clientInfo": {
                    "name": "bridges-idle-probe", "version": "0.1.0"}})
                if "error" in init:
                    raise RuntimeError(init["error"])
                proc.stdin.write('{"method":"initialized"}\n')
                proc.stdin.flush()
                started = request("thread/start", {"ephemeral": True, "cwd": tmp})
                if "error" in started:
                    raise RuntimeError(started["error"])
                thread = started["result"]["thread"]
                # There is no active turn ID in a never-started chat. Supply a
                # valid UUID sentinel to satisfy the required wire field.
                expected = str(uuid.uuid4())
                response = request("turn/steer", {
                    "threadId": thread["id"], "expectedTurnId": expected,
                    "input": [{"type": "text", "text": "Idle steering probe.",
                               "text_elements": []}]})
                record = {"at": datetime.now(timezone.utc).isoformat(),
                          "test": "standalone-app-server-idle-steer",
                          "ephemeral": True, "isolated_home": True,
                          "thread_id": thread["id"],
                          "initial_status": thread.get("status"),
                          "initial_turn_count": len(thread.get("turns", [])),
                          "expected_turn_id": expected, "response": response}
                log = Path(__file__).resolve().parent / ".runtime/app-server-idle.json"
                log.parent.mkdir(exist_ok=True)
                log.write_text(json.dumps(record, indent=2) + "\n")
                print(json.dumps(record, indent=2), flush=True)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
