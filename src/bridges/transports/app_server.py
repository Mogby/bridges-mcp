"""Observe and deliver to application-owned sessions; never launch or resume them."""

import asyncio
import logging

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException

from ..errors import BridgeError
from .base import Delivery, Transport, TurnState
from .rpc import MAX_FRAME, RPC, RPCRejected

log = logging.getLogger(__name__)


class AppServer:
    def __init__(self, config):
        self.config = config
        self.rpc = None
        self.sessions = {}
        self.connect_lock = asyncio.Lock()
        self.session_lock = asyncio.Lock()
        self.monitor = None

    async def connect(self):
        async with self.connect_lock:
            if self.rpc and not self.rpc.closed:
                return
            if self.rpc:
                await self.rpc.close()
                self.rpc = None
            socket = await unix_connect(
                self.config.socket_path,
                uri="ws://localhost",
                max_size=MAX_FRAME,
                open_timeout=3,
                close_timeout=2,
            )
            rpc = RPC(socket, on_event=self.event, on_disconnect=self.disconnected)
            try:
                await rpc.request(
                    "initialize", {"clientInfo": {"name": "bridges", "version": "0.2.0"}}
                )
                await rpc.send({"method": "initialized"})
            except BaseException:
                await rpc.close()
                raise
            self.rpc = rpc
            if self.monitor is None:
                self.monitor = asyncio.create_task(self.observe())

    async def session(self, thread_id):
        async with self.session_lock:
            session = self.sessions.get(thread_id)
            if session is None:
                session = AppServerSession(self, thread_id)
            state = await session.snapshot()
            if state.status not in {"idle", "active"}:
                raise BridgeError(
                    "session_unavailable",
                    "The application must load the calling session in its App Server.",
                )
            self.sessions[thread_id] = session
            return session

    def disconnected(self):
        for session in self.sessions.values():
            session.disconnected()

    def event(self, message):
        session = self.sessions.get(message.get("params", {}).get("threadId"))
        if session:
            session.event(message)

    async def observe(self):
        # thread/read does not subscribe. Poll lifecycle without thread/resume:
        # only the owning application is allowed to load or resume conversations.
        while True:
            await asyncio.sleep(1)
            await asyncio.gather(*(session.snapshot() for session in list(self.sessions.values())))

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.rpc:
            await self.rpc.close()
        self.disconnected()


class AppServerSession(Transport):
    def __init__(self, backend, thread_id):
        super().__init__()
        self.backend, self.thread_id = backend, thread_id
        self.event_revision = 0
        self.pending_requests = {}
        self.snapshot_lock = asyncio.Lock()

    @property
    def rpc(self):
        return self.backend.rpc

    def disconnected(self):
        self.event_revision += 1
        self.update(TurnState("disconnected"))

    def event(self, message):
        method, params = message.get("method", ""), message.get("params", {})
        if params.get("threadId") != self.thread_id:
            return
        if "id" in message:
            # Preserve Codex approval/input policy. Steering never answers these.
            self.pending_requests[message["id"]] = method
            log.warning("codex_request_pending method=%s", method)
            return
        if method == "serverRequest/resolved":
            self.pending_requests.pop(params.get("requestId"), None)
        if method == "turn/started":
            self.event_revision += 1
            self.update(TurnState("active", params["turn"]["id"]))
        elif method == "turn/completed":
            if params["turn"]["id"] == self.state.turn_id:
                self.event_revision += 1
                self.update(TurnState("idle"))
        elif method == "thread/status/changed":
            self.event_revision += 1
            status = params.get("status", {}).get("type")
            if status == "active":
                self.update(TurnState("active", self.state.turn_id))
            elif status == "idle":
                self.update(TurnState("idle"))
            else:
                self.update(TurnState())

    async def snapshot(self):
        async with self.snapshot_lock:
            revision = self.event_revision
            try:
                await self.backend.connect()
                result = await self.rpc.request(
                    "thread/read", {"threadId": self.thread_id, "includeTurns": False}
                )
                if result["thread"]["id"] != self.thread_id:
                    raise KeyError("thread identity mismatch")
                status = result["thread"]["status"]["type"]
                state = TurnState("idle") if status == "idle" else TurnState()
                if status == "active":
                    turns = await self.rpc.request(
                        "thread/turns/list",
                        {
                            "threadId": self.thread_id,
                            "limit": 10,
                            "sortDirection": "desc",
                            "itemsView": "notLoaded",
                        },
                    )
                    active = [
                        turn["id"] for turn in turns["data"] if turn["status"] == "inProgress"
                    ]
                    state = TurnState("active", active[0] if len(active) == 1 else None)
                if revision == self.event_revision:
                    self.update(state)
            except (OSError, TimeoutError, BridgeError, KeyError, TypeError, WebSocketException):
                self.disconnected()
            return self.state

    async def deliver(self, text):
        async with self.delivery_lock:
            for attempt in range(2):
                state = await self.snapshot()
                if state.status not in {"active", "idle"} or (
                    state.status == "active" and not state.turn_id
                ):
                    return Delivery("rejected", "codex_unavailable")
                params = {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": text, "text_elements": []}],
                }
                method = "turn/start"
                if state.status == "active":
                    method = "turn/steer"
                    params["expectedTurnId"] = state.turn_id
                revision = self.event_revision
                try:
                    result = await self.rpc.request(method, params)
                    if method == "turn/start" and revision == self.event_revision:
                        turn = result["turn"]
                        if turn.get("status", "inProgress") == "inProgress":
                            self.update(TurnState("active", turn["id"]))
                    return Delivery("accepted", "steered" if method == "turn/steer" else "started")
                except RPCRejected as exc:
                    error = exc.response.get("error", {})
                    message = error.get("message", "") if isinstance(error, dict) else ""
                    if (
                        attempt == 0
                        and isinstance(error, dict)
                        and error.get("code") == -32600
                        and (
                            message == "no active turn to steer"
                            or message.startswith("expected turn id")
                        )
                    ):
                        continue
                    return Delivery("rejected", "codex_rejected")
                except (OSError, TimeoutError):
                    self.disconnected()
                    return Delivery("unknown", "delivery_unknown")
            return Delivery("rejected", "codex_rejected")
