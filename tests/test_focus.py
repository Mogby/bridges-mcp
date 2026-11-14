import asyncio
from unittest.mock import AsyncMock

from bridges.envelope import Inbound
from bridges.state import InstanceState


async def call(runtime, name, connection="one", thread="chat", **args):
    return await runtime.call(
        name, {"connection_id": connection, **args}, {"threadId": thread, "turnId": "turn-1"}, "app"
    )


async def test_focus_routes_independently_and_survives_restart(runtime):
    await runtime.start()
    assert runtime.focuses == {}
    runtime.enqueue(Inbound("one", "unfocused"))
    await runtime.queue.join()
    assert not runtime.backends["app"].sessions["chat"].delivered
    assert not (await call(runtime, "focus")).isError
    assert not (await call(runtime, "focus", "two")).isError
    assert not (await call(runtime, "focus", thread="other")).isError
    runtime.enqueue(Inbound("one", "first"))
    runtime.enqueue(Inbound("two", "second"))
    await runtime.queue.join()
    sessions = runtime.backends["app"].sessions
    assert sessions["other"].delivered == ["first"]
    assert sessions["chat"].delivered == ["second"]
    saved = InstanceState(runtime.store.root, runtime.config.instance_id).read_focus()
    assert saved == runtime.focuses
    assert saved["one"] == {"client_id": "app", "thread_id": "other"}


async def test_focus_cancels_only_displaced_typing_and_rejects_stale_disable(runtime):
    await runtime.start()
    for connection in ["one", "two"]:
        await call(runtime, "focus", connection)
        assert not (await call(runtime, "telegram_set_typing", connection, enabled=True)).isError
    old = runtime.typing[("app", "chat")]
    assert set(old.owners) == {"one", "two"}
    await call(runtime, "focus", thread="other")
    assert set(old.owners) == {"two"}
    assert not (await call(runtime, "telegram_set_typing", thread="other", enabled=True)).isError
    denied = await call(runtime, "telegram_set_typing", enabled=False)
    assert denied.structuredContent["code"] == "not_focused"
    assert "one" in runtime.typing[("app", "other")].owners
    task = runtime.typing[("app", "other")].owners["one"].task
    await call(runtime, "focus", thread="other")
    assert runtime.typing[("app", "other")].owners["one"].task is task


async def test_failed_or_unauthenticated_focus_preserves_mapping(runtime, monkeypatch):
    await runtime.start()
    await call(runtime, "focus")
    previous = dict(runtime.focuses)
    for metadata, client in [
        ({}, "app"),
        ({"threadId": "other"}, None),
        ({"threadId": "unknown"}, "app"),
    ]:
        result = await runtime.call("focus", {"connection_id": "one"}, metadata, client)
        assert result.isError and runtime.focuses == previous
    assert (await call(runtime, "focus", "missing")).isError

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.store, "save_focus", fail)
    assert (await call(runtime, "focus", thread="other")).isError
    assert runtime.focuses == previous


async def test_concurrent_focus_and_inflight_delivery_are_serialized(runtime):
    await runtime.start()
    await call(runtime, "focus")
    entered, release = asyncio.Event(), asyncio.Event()

    async def deliver(_):
        entered.set()
        await release.wait()
        from bridges.transports.base import Delivery

        return Delivery("accepted", "started")

    runtime.backends["app"].sessions["chat"].deliver = deliver
    runtime.enqueue(Inbound("one", "before"))
    await entered.wait()
    switch = asyncio.create_task(call(runtime, "focus", thread="other"))
    await asyncio.sleep(0)
    assert not switch.done()
    release.set()
    assert not (await switch).isError
    runtime.enqueue(Inbound("one", "after"))
    await runtime.queue.join()
    assert runtime.backends["app"].sessions["other"].delivered == ["after"]
    await asyncio.gather(*(call(runtime, "focus", thread=t) for t in ["chat", "other"] * 20))
    assert runtime.store.read_focus() == runtime.focuses
    assert isinstance(runtime.focuses["one"], dict)


async def test_missing_application_does_not_clear_persisted_focus(runtime):
    await runtime.start()
    await call(runtime, "focus")
    from bridges.errors import BridgeError

    runtime.backends["app"].session = AsyncMock(
        side_effect=BridgeError("session_unavailable", "offline")
    )
    runtime.enqueue(Inbound("one", "drop"))
    await runtime.queue.join()
    assert runtime.focuses["one"]["thread_id"] == "chat"


async def test_client_identity_is_part_of_focus_target(runtime):
    from conftest import FakeBackend

    runtime.backends["second"] = FakeBackend()
    await runtime.start()
    await call(runtime, "focus")
    await call(runtime, "telegram_set_typing", enabled=True)
    switched = await runtime.call("focus", {"connection_id": "one"}, {"threadId": "chat"}, "second")
    assert not switched.isError
    assert runtime.focuses["one"] == {"client_id": "second", "thread_id": "chat"}
    assert not runtime.typing[("app", "chat")].owners
    runtime.enqueue(Inbound("one", "belongs to second application"))
    await runtime.queue.join()
    assert not runtime.backends["app"].sessions["chat"].delivered
    assert runtime.backends["second"].sessions["chat"].delivered == [
        "belongs to second application"
    ]
