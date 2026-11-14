import json
from unittest.mock import AsyncMock

import pytest
from conftest import FakeTransport
from pydantic import ValidationError

from bridges.config import Codex, Config
from bridges.errors import BridgeError
from bridges.runtime import Runtime
from bridges.state import InstanceState
from bridges.transports.app_server import AppServerTransport
from bridges.transports.base import TurnState


def test_app_server_is_only_configured_transport():
    assert Codex().mode == "app_server"
    assert Codex(mode="app_server").mode == "app_server"
    for fields in [{"mode": "desktop"}, {"ipc_path": "/tmp/old.sock"}]:
        with pytest.raises(ValidationError):
            Codex.model_validate(fields)


def test_old_binding_requires_explicit_reset(tmp_path):
    state = InstanceState(tmp_path, "test")
    state.acquire()
    try:
        state.binding_path.write_text(
            json.dumps({"instance_id": "test", "mode": "desktop", "thread_id": "old-chat"})
        )
        with pytest.raises(BridgeError, match="not an App Server binding"):
            state.read()
        state.reset()
        assert state.read() is None
    finally:
        state.close()


async def test_startup_failure_does_not_bind_or_poll(runtime):
    runtime.transport.launch = AsyncMock(
        side_effect=BridgeError("subscription_login_required", "Log in")
    )
    with pytest.raises(BridgeError):
        await runtime.start("http://127.0.0.1:8000/mcp", "test-bearer")
    assert not runtime.ready and runtime.store.read() is None
    assert not runtime.telegram.tasks and runtime.worker is None
    docs = await runtime.call("get_protocol_docs", {}, {"threadId": "other"})
    assert docs.structuredContent["code"] == "not_ready"


async def test_saved_binding_is_resumed_before_polling(config):
    store = InstanceState(config.state_path, config.instance_id)
    store.acquire()
    store.save("saved-chat")
    transport = FakeTransport()
    runtime = Runtime(config, store, transport, {}, {})
    runtime.telegram.start = AsyncMock()
    try:

        async def bind(thread_id):
            assert thread_id == "saved-chat"
            assert not runtime.ready
            runtime.telegram.start.assert_not_awaited()
            return thread_id

        transport.bind = AsyncMock(side_effect=bind)
        await runtime.start("http://127.0.0.1:8000/mcp", "test-bearer")
        await runtime.start("http://127.0.0.1:8000/mcp", "test-bearer")
        transport.bind.assert_awaited_once_with("saved-chat")
        runtime.telegram.start.assert_awaited_once()
        assert runtime.protocol_docs("saved-chat")["binding"]["thread_id"] == "saved-chat"
        denied = await runtime.call("get_protocol_docs", {}, {"threadId": "other"})
        assert denied.structuredContent["code"] == "binding_conflict"
    finally:
        await runtime.close()
        store.close()


async def test_app_server_start_and_resume_wire_methods(config):
    transport = AppServerTransport(config.codex)
    transport.rpc = AsyncMock()
    transport.rpc.request.return_value = {"thread": {"id": "owned-chat"}}
    transport.snapshot = AsyncMock(return_value=TurnState("idle"))
    assert await transport.bind() == "owned-chat"
    assert transport.rpc.request.call_args.args[0] == "thread/start"
    assert await transport.bind("owned-chat") == "owned-chat"
    transport.rpc.request.assert_awaited_with(
        "thread/resume", {"threadId": "owned-chat", "excludeTurns": True}
    )
    with pytest.raises(BridgeError):
        await transport.bind("another-chat")


async def test_unobservable_thread_rejects_startup(config):
    transport = AppServerTransport(config.codex)
    transport.rpc = AsyncMock()
    transport.rpc.request.return_value = {"thread": {"id": "owned-chat"}}
    transport.snapshot = AsyncMock(return_value=TurnState("disconnected"))
    with pytest.raises(BridgeError) as exc:
        await transport.bind()
    assert exc.value.code == "codex_unavailable"


async def test_empty_connections_still_start_an_owned_session(tmp_path):
    config = Config(instance_id="empty", state_dir=str(tmp_path))
    store = InstanceState(config.state_path, config.instance_id)
    store.acquire()
    runtime = Runtime(config, store, FakeTransport(), {}, {})
    try:
        await runtime.start("http://127.0.0.1:8000/mcp", "test-bearer")
        assert runtime.ready and runtime.store.read() == "chat"
        assert not runtime.telegram.tasks
        docs = runtime.protocol_docs("chat")
        assert docs["tools"] == ["get_protocol_docs"] and "telegram" not in docs
    finally:
        await runtime.close()
        store.close()
