#!/usr/bin/env python3
"""Dependency-free, legacy stdio MCP probe for Codex chat binding and delivery."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from desktop_steer import DesktopIPC

THREAD_KEYS = ("openai/threadId", "openai/thread_id", "codexThreadId",
               "codex_thread_id", "threadId", "thread_id")
ENV_KEYS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH",
            "CODEX_APP_RUNTIME_CLI_PATH")


def identify_thread(meta):
    """Only accept explicit host attribution, never infer from cwd or recency."""
    for key in THREAD_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), "metadata:" + key
    value = meta.get("x-codex-turn-metadata", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    if isinstance(value, dict) and isinstance(value.get("thread_id"), str):
        return value["thread_id"], "metadata:x-codex-turn-metadata"
    nested = meta.get("thread", {})
    if isinstance(nested, dict) and isinstance(nested.get("id"), str):
        return nested["id"], "metadata:thread.id"
    return None, None


class Probe:
    def __init__(self, ipc_path, log_dir):
        self.ipc_path = ipc_path
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / ("mcp-" + str(os.getpid()) + ".jsonl")
        self.lock = threading.Lock()
        self.jobs = []
        self.log("startup", environment={k: os.getenv(k) for k in ENV_KEYS},
                 ipc_path=str(ipc_path), delivery_mode="steer", version="0.2.0")

    def log(self, event, **fields):
        with self.lock, self.log_path.open("a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                "event": event, **fields}) + "\n")

    def context(self, params):
        meta = params.get("_meta") or {}
        thread_id, source = identify_thread(meta)
        result = {"thread_id": thread_id, "source": source,
                  "metadata_keys": sorted(meta),
                  "environment_thread_id_present": bool(os.getenv("CODEX_THREAD_ID")),
                  "desktop_socket_env_present": bool(os.getenv("CODEX_APP_TOOLS_PIPE_PATH")),
                  "log_path": str(self.log_path)}
        self.log("tool_call", tool=params.get("name"), **result)
        return result

    def deliver(self, thread_id, marker, delay):
        time.sleep(delay)
        body = ("bridge_context:\n  bridge: poc\n  connection_id: local-test\n"
                f'  received_at: "{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"\n'
                "---\nActive-turn MCP steering test: " + marker
                + ". Acknowledge this marker during the current turn and continue the proof of concept. "
                "Do not schedule another test automatically.")
        try:
            client = DesktopIPC(self.ipc_path)
            try:
                owner = client.owner(thread_id)
                result = client.steer(thread_id, owner, body)
                self.log("steer_result", marker=marker, thread_id=thread_id, response=result)
            finally:
                client.close()
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.log("steer_error", marker=marker, error=str(exc), retried=False,
                     queued_fallback=False)

    def handle(self, method, params):
        if method == "initialize":
            self.log("initialize", client_info=params.get("clientInfo"),
                     metadata_keys=sorted((params.get("_meta") or {})))
            return {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "bridges-poc", "version": "0.2.0"},
                    "instructions": "Local bridge experiment. inspect_context checks which chat called it. "
                                    "steer_test delivers one labeled message into that chat's active turn. "
                                    "This uses experimental desktop IPC; it never falls back to queueing. "
                                    "Only send tests as part of the user-authorized bridge experiment."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [
                {"name": "inspect_context", "description": "Inspect host-provided chat identity without sending messages.",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                 "annotations": {"readOnlyHint": True}},
                {"name": "steer_test", "description": "Steer the calling chat's active turn with one labeled test message. "
                                                          "Experimental desktop IPC; fails if idle and never queues.",
                 "inputSchema": {"type": "object", "properties": {
                     "delay_seconds": {"type": "integer", "minimum": 0, "maximum": 30, "default": 2}},
                     "additionalProperties": False},
                 "annotations": {"readOnlyHint": False, "destructiveHint": False}}
            ]}
        if method == "tools/call":
            if params.get("name") not in ("inspect_context", "steer_test"):
                raise ValueError("Unknown tool")
            context = self.context(params)
            if params["name"] == "inspect_context":
                return {"content": [{"type": "text", "text": json.dumps(context)}]}
            delay = (params.get("arguments") or {}).get("delay_seconds", 2)
            if type(delay) is not int or not 0 <= delay <= 30:
                raise ValueError("delay_seconds must be an integer between 0 and 30")
            if not context["thread_id"]:
                return {"isError": True, "content": [{"type": "text", "text":
                        "Host supplied no chat identity. No message sent; refusing to guess."}]}
            marker = "bridges-mcp-" + uuid.uuid4().hex[:12]
            job = threading.Thread(target=self.deliver,
                                   args=(context["thread_id"], marker, delay), daemon=True)
            job.start()
            self.jobs.append(job)
            return {"content": [{"type": "text", "text": json.dumps({
                "scheduled": True, "delivered": False, "marker": marker,
                "thread_id": context["thread_id"], "delay_seconds": delay})}]}
        raise LookupError("Method not found")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Deprecated; accepted for existing probe registration compatibility")
    parser.add_argument("--ipc-path", type=Path,
                        default=Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "ipc/ipc.sock")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parent / ".runtime")
    args = parser.parse_args()
    probe = Probe(args.ipc_path, args.log_dir)
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            reply["result"] = probe.handle(message["method"], message.get("params") or {})
        except LookupError as exc:
            reply["error"] = {"code": -32601, "message": str(exc)}
        except ValueError as exc:
            reply["error"] = {"code": -32602, "message": str(exc)}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
