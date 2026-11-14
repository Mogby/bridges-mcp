#!/usr/bin/env python3
"""Experimental desktop IPC steering; unsupported, version-specific interface.

Uses the existing same-user IPC router, without modifying the app or its auth.
Discovery is read-only. --steer-test sends exactly one labeled test message.
Never retries an uncertain steering request and never falls back to queueing.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time
import uuid

MAX_FRAME = 16 * 1024 * 1024
VERSIONS = {"initialize": 0, "thread-owner-discovery": 1,
            "thread-follower-steer-turn": 1}


class DesktopIPC:
    def __init__(self, path):
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("IPC endpoint must be a socket owned by the current user")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        try:
            self.sock.connect(str(path))
            self.client_id = "uninitialized"
            result = self.request("initialize", {"clientType": "bridges-poc"})
            self.client_id = result["result"]["clientId"]
        except BaseException:
            self.sock.close()
            raise

    def close(self):
        self.sock.close()

    def send(self, message):
        data = json.dumps(message).encode()
        if len(data) > MAX_FRAME:
            raise ValueError("IPC request too large")
        self.sock.sendall(struct.pack("<I", len(data)) + data)

    def exact(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("IPC response deadline exceeded; delivery may be unknown")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("Desktop IPC connection closed")
            data.extend(chunk)
        return bytes(data)

    def request(self, method, params, target=None):
        request_id = str(uuid.uuid4())
        message = {"type": "request", "requestId": request_id,
                   "sourceClientId": self.client_id, "version": VERSIONS[method],
                   "method": method, "params": params, "timeoutMs": 10000}
        if target:
            message["targetClientId"] = target
        self.send(message)
        deadline = time.monotonic() + 12
        while True:
            size, = struct.unpack("<I", self.exact(4, deadline))
            if size > MAX_FRAME:
                raise ValueError("IPC response too large")
            response = json.loads(self.exact(size, deadline))
            if response.get("type") == "client-discovery-request":
                self.send({"type": "client-discovery-response",
                           "requestId": response["requestId"], "response": {"canHandle": False}})
            elif response.get("type") == "response" and response.get("requestId") == request_id:
                if response.get("resultType") != "success":
                    raise RuntimeError(response.get("error", "IPC request failed"))
                return response
            # Ignore broadcasts without retaining other chats' content.

    def owner(self, thread_id):
        return self.request("thread-owner-discovery", {
            "hostId": "local", "conversationId": thread_id})["handledByClientId"]

    def steer(self, thread_id, owner_id, text, cwd=None):
        message_id = str(uuid.uuid4())
        restore = {"id": message_id, "text": text,
                   "createdAt": int(time.time() * 1000),
                   "context": {"prompt": text, "addedFiles": [], "fileAttachments": [],
                               "ideContext": None, "imageAttachments": []}}
        if cwd is not None:
            restore["cwd"] = cwd
            restore["context"]["workspaceRoots"] = [cwd]
        # The owning desktop client looks up its actual active turn ID and
        # passes expectedTurnId to App Server. No model/permission overrides.
        return self.request("thread-follower-steer-turn", {
            "conversationId": thread_id, "clientUserMessageId": message_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
            "attachments": [], "restoreMessage": restore}, target=owner_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path,
                        default=Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "ipc/ipc.sock")
    parser.add_argument("--thread-id", default=os.getenv("CODEX_THREAD_ID"))
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--steer-test", action="store_true")
    args = parser.parse_args()
    if not args.thread_id:
        parser.error("An explicit thread ID is required; no recency-based guessing")
    client = DesktopIPC(args.socket)
    try:
        owner = client.owner(args.thread_id)
        print(json.dumps({"owner_found": True, "thread_id": args.thread_id,
                          "owner_client_id": owner}), flush=True)
        if args.steer_test:
            marker = "bridges-steer-" + uuid.uuid4().hex[:12]
            body = ("bridge_context:\n  bridge: poc\n  connection_id: local-test\n"
                    f'  received_at: "{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"\n'
                    "---\nActive-turn bridge steering test: " + marker
                    + ". Acknowledge this marker during the current turn and continue the experiment. "
                    "Do not schedule another test automatically.")
            result = client.steer(args.thread_id, owner, body, args.cwd)
            print(json.dumps({"marker": marker, "steer_result": result}), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
