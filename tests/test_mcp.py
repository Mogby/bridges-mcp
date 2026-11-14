import asyncio
import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bridges.server import create_server


async def test_sdk_roundtrip_metadata_discovery_and_errors(runtime):
    server = create_server(runtime)
    client_send, server_receive = anyio.create_memory_object_stream(10)
    server_send, client_receive = anyio.create_memory_object_stream(10)
    task = asyncio.create_task(
        server.run(server_receive, server_send, server.create_initialization_options())
    )
    try:
        async with ClientSession(client_receive, client_send) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "bridges"
            assert "Ordinary assistant" in initialized.instructions
            listing = await client.list_tools()
            assert len(listing.tools) == 7
            docs = await client.call_tool("get_protocol_docs", meta={"threadId": "chat"})
            assert not docs.isError and docs.structuredContent["binding"]["thread_id"] == "chat"
            denied = await client.call_tool(
                "telegram_send_text",
                {"text": "private", "connection_id": "one"},
                meta={"threadId": "wrong"},
            )
            assert denied.isError and denied.structuredContent["code"] == "binding_conflict"
            invalid = await client.call_tool(
                "telegram_set_typing", {"enabled": "true"}, meta={"threadId": "chat"}
            )
            assert invalid.isError and invalid.structuredContent["code"] == "invalid_payload"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stdio_process_handshake_and_empty_tools(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"instance_id: empty\nstate_dir: {tmp_path}\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bridges.cli", "serve", "--config", str(config)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        listing = await client.list_tools()
        assert [tool.name for tool in listing.tools] == ["get_protocol_docs"]
        result = await client.call_tool("get_protocol_docs")
        assert result.isError and json.loads(result.content[0].text)["code"] == "binding_conflict"
