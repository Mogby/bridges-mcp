#!/usr/bin/env python3
"""Probe the installed desktop app's private app-tools socket.

Read-only. This is an experiment, not a supported Codex API.
No credentials, config changes, or conversation-history scraping.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import sys
import uuid

MAX_FRAME = 8 * 1024 * 1024


def receive_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise ConnectionError("Desktop closed the connection")
        result.extend(chunk)
    return bytes(result)


def request(sock, method, params):
    request_id = str(uuid.uuid4())
    body = json.dumps({"jsonrpc": "2.0", "id": request_id,
                       "method": method, "params": params}).encode()
    if len(body) > MAX_FRAME:
        raise ValueError("Request exceeds frame limit")
    sock.sendall(struct.pack("<I", len(body)) + body)
    size, = struct.unpack("<I", receive_exact(sock, 4))
    if size > MAX_FRAME:
        raise ValueError("Response exceeds frame limit")
    response = json.loads(receive_exact(sock, size))
    if response.get("id") != request_id:
        raise ValueError("Response ID mismatch")
    if "error" in response:
        raise RuntimeError(json.dumps(response["error"]))
    return response["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=os.getenv("CODEX_APP_TOOLS_PIPE_PATH"))
    parser.add_argument("--thread-id", default=os.getenv("CODEX_THREAD_ID"))
    args = parser.parse_args()
    print(json.dumps({"thread_id": args.thread_id,
                      "socket": args.socket,
                      "socket_exists": bool(args.socket and Path(args.socket).is_socket())}))
    if not args.socket:
        parser.error("No desktop socket supplied by the environment; use --socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(15)
        sock.connect(args.socket)
        catalog = request(sock, "tools/list", {"threadStartKind": "all"})
        matches = [t for t in catalog["tools"] if t["name"] == "send_message_to_thread"]
        print(json.dumps({"connected": True, "send_tool": matches}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
