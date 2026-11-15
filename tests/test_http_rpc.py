import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route
from websockets.asyncio.server import unix_serve

from bridges.cli import BearerEndpoint
from bridges.config import Client
from bridges.server import create_server
from bridges.transports.app_server import AppServer, AppServerSession
from bridges.transports.base import TurnState
from bridges.transports.rpc import RPC


async def test_http_auth_focus_and_host_identity(runtime):
    await runtime.start()
    manager = StreamableHTTPSessionManager(
        create_server(runtime), json_response=True, stateless=True
    )
    app = Starlette(
        routes=[
            Route(
                "/mcp",
                BearerEndpoint(manager, {"app": "test-bearer"}),
                methods=["POST", "GET", "DELETE"],
            )
        ]
    )
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert (await client.post("/mcp", json=message)).status_code == 401
        headers = {
            "Authorization": "Bearer test-bearer",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        listed = (await client.post("/mcp", json=message, headers=headers)).json()["result"][
            "tools"
        ]
        assert len(listed) == 6
        assert "focus" not in {tool["name"] for tool in listed}

        async def call(name, args, meta):
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args, "_meta": meta},
                },
            )
            return response.json()["result"]

        result = await call(
            "telegram_send_text",
            {"connection_id": "one", "text": "hi"},
            {"threadId": "chat", "client_id": "attacker"},
        )
        assert not result["isError"]
        assert (await call("telegram_send_text", {"connection_id": "one", "text": "hi"}, {}))[
            "isError"
        ]
        assert (await call("focus", {"connection_id": "one"}, {"threadId": "chat"}))["isError"]
        docs = await call("get_protocol_docs", {}, {"threadId": "chat"})
        assert docs["structuredContent"]["sessions"]["shared"]["thread_id"] == "chat"
        assert "focus" not in docs["structuredContent"]["tools"]


async def test_websocket_rpc_multiplexing_and_disconnect(socket_path):
    path = socket_path
    notifications, lost = [], []

    async def handler(socket):
        request = json.loads(await socket.recv())
        await socket.send(json.dumps({"method": "turn/started", "params": {}}))
        await socket.send(json.dumps({"id": request["id"], "result": {"ok": True}}))
        await socket.recv()

    from websockets.asyncio.client import unix_connect

    async with unix_serve(handler, path):
        socket = await unix_connect(path, uri="ws://localhost")
        rpc = RPC(socket, on_event=notifications.append, on_disconnect=lambda: lost.append(True))
        assert await rpc.request("method", {}) == {"ok": True}
        assert notifications
        with pytest.raises(ConnectionError):
            await rpc.request("lost", {})
        assert lost
        await rpc.close()


async def test_completed_turn_not_resurrected_by_start_ack():
    transport = AppServerSession(AsyncMock(), "chat")
    transport.snapshot = AsyncMock(return_value=TurnState("idle"))

    async def request(method, params):
        transport.event(
            {"method": "turn/started", "params": {"threadId": "chat", "turn": {"id": "fast"}}}
        )
        transport.event(
            {"method": "turn/completed", "params": {"threadId": "chat", "turn": {"id": "fast"}}}
        )
        return {"turn": {"id": "fast", "status": "inProgress"}}

    transport.backend.rpc.request.side_effect = request
    delivered = await transport.deliver("hello")
    assert delivered.outcome == "accepted" and delivered.turn_id == "fast"
    assert transport.state == TurnState("idle")


async def test_independent_app_server_disconnect_reconnect_and_no_resume(socket_path):
    path = socket_path
    methods = []

    async def handler(socket):
        async for line in socket:
            request = json.loads(line)
            method = request["method"]
            methods.append(method)
            if "id" not in request:
                continue
            if method == "initialize":
                result = {}
            elif method == "thread/read":
                thread = request["params"]["threadId"]
                result = {
                    "thread": {
                        "id": thread,
                        "status": {"type": "notLoaded" if thread == "offline" else "idle"},
                    }
                }
            elif method == "turn/start":
                result = {"turn": {"id": "turn", "status": "inProgress"}}
            else:
                raise AssertionError(method)
            await socket.send(json.dumps({"id": request["id"], "result": result}))

    backend = AppServer(Client(token_env="TOKEN", socket_path=path))
    try:
        for _ in range(2):
            async with unix_serve(handler, path):
                session = await backend.session("one")
                assert (await session.deliver("text")).outcome == "accepted"
                from bridges.errors import BridgeError

                with pytest.raises(BridgeError):
                    await backend.session("offline")
            await asyncio.sleep(0.01)
            assert session.state.status == "disconnected"
        assert "thread/start" not in methods and "thread/resume" not in methods
    finally:
        await backend.close()
