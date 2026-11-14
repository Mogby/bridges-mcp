"""Bridge-owned App Server using the installed v2 JSON-RPC protocol."""

import asyncio
import json
import logging
import os

from ..errors import BridgeError
from .base import Delivery, Transport, TurnState
from .rpc import RPC, RPCRejected

log = logging.getLogger(__name__)


class AppServerTransport(Transport):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.proc = None
        self.rpc = None
        self.event_revision = 0
        self.pending_requests = {}
        self.snapshot_lock = asyncio.Lock()

    def disconnected(self):
        self.event_revision += 1
        self.update(TurnState("disconnected"))

    async def launch(self, url, bearer):
        env = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(key, None)
        env["BRIDGES_MCP_BEARER"] = bearer
        # A single loopback endpoint; never spawn another listener process.
        override = (
            "mcp_servers.bridges="
            + "{url="
            + json.dumps(url)
            + ',bearer_token_env_var="BRIDGES_MCP_BEARER"}'
        )
        self.proc = await asyncio.create_subprocess_exec(
            self.config.executable,
            "-c",
            override,
            "app-server",
            "--stdio",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
        )
        self.rpc = RPC(
            self.proc.stdout, self.proc.stdin, on_event=self.event, on_disconnect=self.disconnected
        )
        await self.rpc.request(
            "initialize", {"clientInfo": {"name": "bridges", "version": "0.1.0"}}
        )
        await self.rpc.send({"method": "initialized"})
        account = await self.rpc.request("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise BridgeError(
                "subscription_login_required",
                "Run codex login on this host with your ChatGPT subscription before starting Bridges.",
            )

    async def bind(self, thread_id=None):
        if self.thread_id and self.thread_id != thread_id:
            raise BridgeError("binding_conflict", "App Server is already bound to another chat.")
        if thread_id:
            result = await self.rpc.request(
                "thread/resume", {"threadId": thread_id, "excludeTurns": True}
            )
        else:
            params = {
                "developerInstructions": "At the start of this chat call bridges.get_protocol_docs and discover Bridges tools. External replies require Bridges tools; ordinary assistant prose is not forwarded."
            }
            if self.config.cwd:
                params["cwd"] = self.config.cwd
            result = await self.rpc.request("thread/start", params)
        self.thread_id = result["thread"]["id"]
        await self.snapshot()
        return self.thread_id

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
                result = await self.rpc.request(
                    "thread/read", {"threadId": self.thread_id, "includeTurns": False}
                )
                status = result["thread"]["status"]["type"]
                if status == "notLoaded":
                    await self.rpc.request(
                        "thread/resume", {"threadId": self.thread_id, "excludeTurns": True}
                    )
                    result = await self.rpc.request(
                        "thread/read", {"threadId": self.thread_id, "includeTurns": False}
                    )
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
            except (OSError, TimeoutError, BridgeError, KeyError, TypeError):
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

    async def close(self):
        if self.rpc:
            await self.rpc.close()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self.disconnected()
