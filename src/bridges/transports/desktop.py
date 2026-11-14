"""Version-specific same-user desktop coordination transport.

Observe the owner's initial snapshot and revisioned Immer patches. Never acquire
ownership, use the app-tools socket, or read history on a typing refresh.
"""

import asyncio
import copy
import os
import stat
import time
import uuid
from pathlib import Path

from ..errors import BridgeError
from .base import Delivery, Transport, TurnState
from .rpc import RPC, RPCRejected


def apply_patches(document, patches):
    for patch in patches:
        path, op = patch["path"], patch["op"]
        if not path:
            if op not in {"add", "replace"}:
                raise ValueError("root patch")
            document = copy.deepcopy(patch["value"])
            continue
        parent = document
        for key in path[:-1]:
            parent = parent[key]
        key = path[-1]
        if op == "remove":
            del parent[key]
        elif op in {"add", "replace"}:
            value = copy.deepcopy(patch["value"])
            if isinstance(parent, list) and op == "add":
                parent.insert(key, value)
            else:
                parent[key] = value
        else:
            raise ValueError("unsupported patch")
    return document


def desktop_state(document):
    runtime = (document.get("threadRuntimeStatus") or {}).get("type")
    if document.get("resumeState") in {"needs_resume", "resuming"}:
        return TurnState()
    turns = document.get("turns", [])
    history = document.get("turnHistory", {})
    if history.get("kind") == "canonical":
        # Entities include turns from multiple history islands. Only an explicit
        # inProgress turn can own typing; runtime idle overrides stale history.
        entities = history.get("history", {}).get("entitiesByKey", {})
        turns = list(entities.values()) + turns
    active = {
        t.get("turnId", t.get("id"))
        for t in turns
        if isinstance(t, dict) and t.get("status") == "inProgress"
    }
    active.discard(None)
    pending = any(not t.get("terminal") for t in document.get("unconfirmedTurnSubmissions", []))
    if runtime == "idle" and not pending:
        return TurnState("idle")
    if len(active) == 1:
        return TurnState("active", next(iter(active)))
    if runtime == "active" or pending:
        return TurnState("active")
    return TurnState()


class DesktopTransport(Transport):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.rpc = None
        self.owner = None
        self.document = None
        self.revision = None
        self.ready = asyncio.Event()
        self.connect_lock = asyncio.Lock()
        self.snapshot_lock = asyncio.Lock()
        self.last_owner_check = 0

    def disconnected(self):
        self.document = None
        self.revision = None
        self.update(TurnState("disconnected"))
        self.ready.clear()

    async def bind(self, thread_id):
        if self.thread_id and self.thread_id != thread_id:
            raise BridgeError("binding_conflict", "Desktop transport is bound to another chat.")
        self.thread_id = thread_id
        async with self.connect_lock:
            if self.rpc:
                await self.rpc.close()
            path = Path(self.config.ipc_path).expanduser()
            try:
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError()
                reader, writer = await asyncio.open_unix_connection(path, limit=16 * 1024 * 1024)
                self.rpc = RPC(
                    reader,
                    writer,
                    framed=True,
                    on_event=self.event,
                    on_disconnect=self.disconnected,
                )
                response = await self.rpc.request(
                    "initialize", {"clientType": "bridges"}, version=0
                )
                self.rpc.client_id = response["result"]["clientId"]
                await self.find_owner()
                self.ready.clear()
                await self.follow(True)
                await asyncio.wait_for(self.ready.wait(), 10)
                if self.state.status in {"unknown", "disconnected"}:
                    raise ValueError("unsupported state")
            except (OSError, TimeoutError, ValueError, KeyError, RPCRejected):
                if self.rpc:
                    await self.rpc.close()
                    self.rpc = None
                self.disconnected()
                raise BridgeError(
                    "desktop_unavailable",
                    "Cannot observe the bound desktop chat. Open it in the desktop app; check IPC compatibility and socket ownership.",
                ) from None

    async def find_owner(self):
        response = await self.rpc.request(
            "thread-owner-discovery", {"hostId": "local", "conversationId": self.thread_id}
        )
        owner = response["handledByClientId"]
        if self.owner and self.owner != owner:
            self.disconnected()
        self.owner = owner
        self.last_owner_check = time.monotonic()

    async def follow(self, enabled):
        await self.rpc.broadcast(
            "thread-stream-following-changed",
            {"hostId": "local", "conversationId": self.thread_id, "following": enabled},
            self.owner,
        )

    def event(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "ipc-connection-reset":
            self.disconnected()
            return
        if method == "client-status-changed":
            if params.get("clientId") == self.owner and params.get("status") in {
                "disconnected",
                "closed",
            }:
                self.disconnected()
            return
        if params.get("conversationId") != self.thread_id or params.get("hostId") != "local":
            return
        if message.get("sourceClientId") != self.owner:
            return
        if method == "thread-stream-following-status-requested":
            # Heartbeat/reconnect discovery: a subsequent snapshot is required
            # after disconnect; do not fabricate lifecycle state.
            return
        if method != "thread-stream-state-changed":
            return
        if message.get("version") != 13:
            self.disconnected()
            return
        change = params.get("change", {})
        try:
            if change["type"] == "snapshot":
                self.document = change["conversationState"]
            elif (
                change["type"] == "patches"
                and self.document is not None
                and change["baseRevision"] == self.revision
            ):
                self.document = apply_patches(self.document, change["patches"])
            else:
                raise ValueError("revision gap")
            self.revision = change["revision"]
            self.update(desktop_state(self.document))
            self.ready.set()
        except (ValueError, TypeError, KeyError, IndexError):
            self.disconnected()

    async def snapshot(self):
        async with self.snapshot_lock:
            return await self._snapshot()

    async def _snapshot(self):
        try:
            if self.state.status in {"unknown", "disconnected"}:
                await self.bind(self.thread_id)
            elif time.monotonic() - self.last_owner_check > 3:
                await self.find_owner()
                if not self.ready.is_set():
                    await self.bind(self.thread_id)
        except (BridgeError, OSError, TimeoutError):
            self.disconnected()
        return self.state

    async def deliver(self, text):
        async with self.delivery_lock:
            for attempt in range(2):
                state = await self.snapshot()
                if state.status not in {"active", "idle"}:
                    return Delivery("rejected", "desktop_unavailable")
                if state.status == "idle":
                    return await self.queue(text)
                message_id = str(uuid.uuid4())
                restore = {
                    "id": message_id,
                    "text": text,
                    "createdAt": int(time.time() * 1000),
                    "context": {
                        "prompt": text,
                        "addedFiles": [],
                        "fileAttachments": [],
                        "ideContext": None,
                        "imageAttachments": [],
                    },
                }
                try:
                    await self.rpc.request(
                        "thread-follower-steer-turn",
                        {
                            "conversationId": self.thread_id,
                            "clientUserMessageId": message_id,
                            "input": [{"type": "text", "text": text, "text_elements": []}],
                            "attachments": [],
                            "restoreMessage": restore,
                        },
                        target=self.owner,
                    )
                    return Delivery("accepted", "steered")
                except RPCRejected as exc:
                    # Only the exact known pre-submission failure permits retry.
                    error = exc.response.get("error")
                    if attempt == 0 and error == "no active turn to steer":
                        self.disconnected()
                        continue
                    return Delivery("rejected", "codex_rejected")
                except (OSError, TimeoutError):
                    self.disconnected()
                    return Delivery("unknown", "delivery_unknown")
            return Delivery("rejected", "codex_rejected")

    async def queue(self, text):
        # This check + CLI submission has a documented narrow active-turn race.
        # Never invoke after a steering timeout or other unknown result.
        try:
            proc = await asyncio.create_subprocess_exec(
                self.config.executable,
                "queue",
                "--thread",
                self.thread_id,
                "--message",
                text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return Delivery("rejected", "codex_unavailable")
        try:
            code = await asyncio.wait_for(proc.wait(), 15)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return Delivery("unknown", "delivery_unknown")
        return (
            Delivery("accepted", "queued_idle")
            if code == 0
            else Delivery("unknown", "delivery_unknown")
        )

    async def close(self):
        if self.rpc:
            try:
                await self.follow(False)
            except (OSError, BridgeError):
                pass
            await self.rpc.close()
            self.rpc = None
        self.disconnected()
