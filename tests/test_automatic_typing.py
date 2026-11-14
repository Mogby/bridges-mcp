import asyncio
from unittest.mock import AsyncMock

import pytest

from bridges.envelope import Inbound
from bridges.errors import BridgeError
from bridges.transports.base import Delivery, TurnState


async def focus(runtime, connection="one"):
    result = await runtime.call("focus", {"connection_id": connection}, {"threadId": "chat"}, "app")
    assert not result.isError


async def receive(runtime, connection="one"):
    runtime.enqueue(Inbound(connection, "hello"))
    await asyncio.wait_for(runtime.queue.join(), 1)
    await asyncio.sleep(0)


async def test_inbound_starts_typing_without_an_agent_tool_call(runtime):
    await runtime.start()
    await focus(runtime)
    runtime.telegram.typing = AsyncMock()
    await receive(runtime)
    runtime.telegram.typing.assert_awaited_once_with("one")
    manager = runtime.typing[("app", "chat")]
    owner = manager.owners["one"]
    await receive(runtime)
    assert manager.owners["one"] is owner
    runtime.telegram.typing.assert_awaited_once()
    runtime.backends["app"].sessions["chat"].update(TurnState("idle"))
    await asyncio.sleep(0)
    assert not manager.owners and owner.task.done()


@pytest.mark.parametrize("outcome", ["rejected", "unknown"])
async def test_failed_delivery_never_starts_typing(runtime, outcome):
    await runtime.start()
    await focus(runtime)
    runtime.telegram.typing = AsyncMock()
    runtime.backends["app"].sessions["chat"].deliver = AsyncMock(
        return_value=Delivery(outcome, "failed")
    )
    await receive(runtime)
    assert not runtime.typing
    runtime.telegram.typing.assert_not_called()


@pytest.mark.parametrize("state", [TurnState("idle"), TurnState("active", "newer")])
async def test_late_delivery_ack_does_not_type_for_completed_or_replacement_turn(runtime, state):
    await runtime.start()
    await focus(runtime)
    runtime.telegram.typing = AsyncMock()
    session = runtime.backends["app"].sessions["chat"]

    async def deliver(_):
        session.update(state)
        return Delivery("accepted", "started", "old")

    session.deliver = deliver
    await receive(runtime)
    assert not runtime.typing[("app", "chat")].owners
    runtime.telegram.typing.assert_not_called()


async def test_slow_typing_does_not_block_other_messages_or_focus_transfer(runtime):
    await runtime.start()
    for connection in ["one", "two"]:
        await focus(runtime, connection)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_typing(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.telegram.typing = slow_typing
    await receive(runtime)
    await entered.wait()
    await receive(runtime, "two")
    assert runtime.backends["app"].sessions["chat"].delivered == ["hello", "hello"]
    switched = await asyncio.wait_for(
        runtime.call("focus", {"connection_id": "one"}, {"threadId": "other"}, "app"), 1
    )
    assert not switched.isError and cancelled.is_set()
    assert set(runtime.typing[("app", "chat")].owners) == {"two"}


@pytest.mark.parametrize(
    "error", [BridgeError("rate_limited", "limited", retry_after=8), OSError()]
)
async def test_typing_failure_does_not_fail_delivery_and_is_retried(runtime, error):
    await runtime.start()
    await focus(runtime)
    runtime.telegram.typing = AsyncMock(side_effect=[error, None])
    manager = runtime.typing_for("app", runtime.backends["app"].sessions["chat"])
    retry, delays = asyncio.Event(), []

    async def sleep(seconds):
        delays.append(seconds)
        await retry.wait()
        retry.clear()

    manager.sleep = sleep
    await receive(runtime)
    assert runtime.backends["app"].sessions["chat"].delivered == ["hello"]
    assert "one" in manager.owners
    owner = manager.owners["one"]
    assert delays == [8 if isinstance(error, BridgeError) else 4]
    retry.set()
    await asyncio.sleep(0)
    assert runtime.telegram.typing.await_count == 2
    assert manager.owners["one"] is owner


async def test_removed_typing_tool_is_neither_discovered_nor_callable(runtime):
    await runtime.start()
    docs = runtime.protocol_docs("chat")
    assert "telegram_set_typing" not in docs["tools"]
    assert "automatically" in docs["telegram"]["typing"]
    result = await runtime.call(
        "telegram_set_typing",
        {"connection_id": "one", "enabled": True},
        {"threadId": "chat"},
        "app",
    )
    assert result.isError and result.structuredContent["code"] == "unknown_tool"


async def test_shutdown_cancels_typing_before_closing_telegram(runtime):
    await runtime.start()
    await focus(runtime)
    runtime.telegram.typing = AsyncMock()
    await receive(runtime)
    manager = runtime.typing[("app", "chat")]
    owner = manager.owners["one"]
    close = runtime.telegram.close

    async def close_telegram():
        assert not manager.owners and owner.task.done()
        await close()

    runtime.telegram.close = close_telegram
    await runtime.close()
