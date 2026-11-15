import asyncio

import anyio
from mcp import ClientSession

from bridges.server import create_server


async def test_sdk_schema_and_no_unauthenticated_tools(runtime):
    await runtime.start()
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
            assert "focus(" not in initialized.instructions
            assert len((await client.list_tools()).tools) == 12
            denied = await client.call_tool(
                "telegram_send_text",
                {"connection_id": "one", "text": "hi"},
                meta={"threadId": "chat", "client_id": "app"},
            )
            assert denied.isError and denied.structuredContent["code"] == "unauthorized"
            invalid = await client.call_tool("telegram_send_text", {}, meta={"threadId": "chat"})
            assert invalid.isError and invalid.structuredContent["code"] == "invalid_payload"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
