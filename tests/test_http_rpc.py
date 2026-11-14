import asyncio
import json
from unittest.mock import AsyncMock

import httpx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

from bridges.cli import BearerEndpoint
from bridges.server import create_server
from bridges.transports.app_server import AppServerTransport
from bridges.transports.base import TurnState
from bridges.transports.rpc import RPC


async def test_loopback_endpoint_auth_and_mcp_callbacks(runtime):
    await runtime.start("http://127.0.0.1:8000/mcp", "test-bearer")
    manager = StreamableHTTPSessionManager(
        create_server(runtime), json_response=True, stateless=True
    )
    app = Starlette(
        routes=[
            Route("/mcp", BearerEndpoint(manager, "test-bearer"), methods=["POST", "GET", "DELETE"])
        ]
    )
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        denied = await client.post("/mcp", json=message)
        assert denied.status_code == 401
        headers = {
            "Authorization": "Bearer test-bearer",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        listed = await client.post("/mcp", json=message, headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()["result"]["tools"]) == 7
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_protocol_docs",
                    "arguments": {},
                    "_meta": {"threadId": "chat"},
                },
            },
            headers=headers,
        )
        assert not response.json()["result"]["isError"]
        rejected = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "telegram_send_text",
                    "arguments": {"text": "no", "connection_id": "one"},
                    "_meta": {"threadId": "other"},
                },
            },
            headers=headers,
        )
        assert rejected.json()["result"]["isError"]


class Writer:
    def __init__(self):
        self.data = []

    def write(self, data):
        self.data.append(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


async def test_rpc_notification_multiplexing_and_lost_response():
    reader, writer, notifications, lost = asyncio.StreamReader(), Writer(), [], []
    rpc = RPC(
        reader, writer, on_event=notifications.append, on_disconnect=lambda: lost.append(True)
    )
    request = asyncio.create_task(rpc.request("method", {}))
    await asyncio.sleep(0)
    request_id = json.loads(writer.data[0])["id"]
    reader.feed_data(json.dumps({"method": "turn/started", "params": {}}).encode() + b"\n")
    reader.feed_data(json.dumps({"id": request_id, "result": {"ok": True}}).encode() + b"\n")
    assert await request == {"ok": True} and len(notifications) == 1
    pending = asyncio.create_task(rpc.request("other", {}))
    await asyncio.sleep(0)
    reader.feed_eof()
    import pytest

    with pytest.raises(ConnectionError):
        await pending
    assert lost
    await rpc.close()


async def test_completed_turn_not_resurrected_by_start_ack(config):
    transport = AppServerTransport(config.codex)
    transport.thread_id = "chat"
    transport.snapshot = AsyncMock(return_value=TurnState("idle"))

    async def request(method, params):
        transport.event(
            {"method": "turn/started", "params": {"threadId": "chat", "turn": {"id": "fast"}}}
        )
        transport.event(
            {"method": "turn/completed", "params": {"threadId": "chat", "turn": {"id": "fast"}}}
        )
        return {"turn": {"id": "fast", "status": "inProgress"}}

    transport.rpc = AsyncMock()
    transport.rpc.request.side_effect = request
    assert (await transport.deliver("hello")).outcome == "accepted"
    assert transport.state == TurnState("idle")
