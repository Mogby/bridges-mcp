import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import FakeTransport

from bridges.errors import BridgeError
from bridges.transports.app_server import AppServerSession
from bridges.transports.base import TurnState
from bridges.transports.rpc import RPCRejected
from bridges.typing import TypingManager


class Clock:
    def __init__(self):
        self.waiting = []

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiting.append((seconds, future))
        await future

    async def tick(self):
        for _ in range(4):
            await asyncio.sleep(0)
        waiting, self.waiting = self.waiting, []
        for _, future in waiting:
            if not future.done():
                future.set_result(None)
        for _ in range(4):
            await asyncio.sleep(0)


async def test_typing_idempotence_two_connections_completion_and_disconnect():
    transport, send, clock = FakeTransport(), AsyncMock(), Clock()
    manager = TypingManager(transport, send, clock.sleep)
    await manager.set("one", True)
    await manager.set("one", True)
    await manager.set("two", True)
    assert send.await_count == 2
    await clock.tick()
    await clock.tick()
    assert send.await_count == 6
    await manager.set("one", False)
    await manager.set("one", False)
    await clock.tick()
    assert send.await_count == 7
    transport.update(TurnState("active", "turn-2"))
    await clock.tick()
    assert not manager.owners and send.await_count == 7
    await manager.set("two", True)
    transport.update(TurnState("disconnected"))
    await clock.tick()
    assert not manager.owners and send.await_count == 8
    await manager.close()


@pytest.mark.parametrize("state", [TurnState("idle"), TurnState("unknown"), TurnState("active")])
async def test_typing_requires_observable_active_turn(state):
    transport, send = FakeTransport(), AsyncMock()
    transport.update(state)
    manager = TypingManager(transport, send)
    with pytest.raises(BridgeError):
        await manager.set("one", True)
    send.assert_not_called()
    await manager.close()


async def test_typing_cancellation_waits_for_inflight_refresh():
    clock, transport = Clock(), FakeTransport()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(connection):
        calls.append(connection)
        if len(calls) == 2:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    manager = TypingManager(transport, send, clock.sleep)
    await manager.set("one", True)
    await clock.tick()
    await entered.wait()
    await manager.set("one", False)
    assert cancelled.is_set()
    await clock.tick()
    assert len(calls) == 2
    await manager.close()


async def test_outbound_send_does_not_stop_typing(runtime):
    await runtime.start()
    await runtime.call("focus", {"connection_id": "one"}, {"threadId": "chat"}, "app")
    enabled = await runtime.call(
        "telegram_set_typing",
        {"connection_id": "one", "enabled": True},
        {"threadId": "chat", "turnId": "turn-1"},
        "app",
    )
    assert not enabled.isError
    await runtime.call(
        "telegram_send_text", {"connection_id": "one", "text": "done"}, {"threadId": "chat"}, "app"
    )
    assert "one" in runtime.typing[("app", "chat")].owners
    runtime.backends["app"].sessions["chat"].update(TurnState("idle"))
    assert not runtime.typing[("app", "chat")].owners


async def test_stale_completion_cannot_stop_new_turn_typing(config):
    transport = AppServerSession(AsyncMock(), "chat")
    transport.snapshot = AsyncMock(side_effect=lambda: transport.state)
    transport.update(TurnState("active", "new"))
    manager = TypingManager(transport, AsyncMock())
    await manager.set("one", True)
    transport.event(
        {
            "method": "turn/completed",
            "params": {"threadId": "chat", "turn": {"id": "old", "status": "completed"}},
        }
    )
    assert transport.state.turn_id == "new" and "one" in manager.owners
    transport.event(
        {
            "method": "turn/completed",
            "params": {"threadId": "chat", "turn": {"id": "new", "status": "interrupted"}},
        }
    )
    assert not manager.owners
    await manager.close()


async def test_app_server_active_idle_and_known_race(config):
    transport = AppServerSession(AsyncMock(), "chat")
    transport.backend.rpc = AsyncMock()
    transport.snapshot = AsyncMock(side_effect=[TurnState("active", "old"), TurnState("idle")])
    transport.rpc.request.side_effect = [
        RPCRejected({"error": {"code": -32600, "message": "no active turn to steer"}}),
        {"turn": {"id": "new"}},
    ]
    delivered = await transport.deliver("body")
    assert delivered.outcome == "accepted"
    assert [call.args[0] for call in transport.rpc.request.call_args_list] == [
        "turn/steer",
        "turn/start",
    ]
    assert transport.rpc.request.call_args_list[0].args[1]["expectedTurnId"] == "old"


async def test_app_server_unknown_response_no_retry(config):
    transport = AppServerSession(AsyncMock(), "chat")
    transport.backend.rpc = AsyncMock()
    transport.snapshot = AsyncMock(return_value=TurnState("idle"))
    transport.rpc.request.side_effect = TimeoutError()
    assert (await transport.deliver("body")).outcome == "unknown"
    transport.rpc.request.assert_awaited_once()


@pytest.mark.parametrize("latest_status", ["inProgress", "completed"])
async def test_snapshot_ignores_stale_in_progress_history(latest_status):
    backend = AsyncMock()
    transport = AppServerSession(backend, "chat")
    history = [
        {"id": "current", "status": latest_status},
        {"id": "before-restart", "status": "inProgress"},
    ]

    async def request(method, params):
        if method == "thread/read":
            return {"thread": {"id": "chat", "status": {"type": "active"}}}
        assert method == "thread/turns/list"
        assert params["sortDirection"] == "desc"
        return {"data": history[: params["limit"]]}

    backend.rpc.request.side_effect = request
    turn = "current" if latest_status == "inProgress" else None
    assert await transport.snapshot() == TurnState("active", turn)
    assert await transport.verify_turn("current") == turn
    assert await transport.verify_turn("before-restart") is None


async def test_approval_and_input_requests_preserve_active_state(config):
    transport = AppServerSession(AsyncMock(), "chat")
    transport.update(TurnState("active", "turn"))
    transport.backend.rpc = AsyncMock()
    for method in ["item/commandExecution/requestApproval", "item/tool/requestUserInput"]:
        transport.event(
            {"id": method, "method": method, "params": {"threadId": "chat", "turnId": "turn"}}
        )
    assert transport.state == TurnState("active", "turn")
    assert len(transport.pending_requests) == 2
    transport.rpc.send.assert_not_called()
