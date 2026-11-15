import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bridges.config import Config
from bridges.envelope import Inbound
from bridges.errors import BridgeError
from bridges.sessions import Sessions


async def receive(runtime, *envelopes):
    for envelope in envelopes:
        runtime.enqueue(envelope)
    await asyncio.wait_for(runtime.queue.join(), 2)


async def test_first_messages_share_one_new_thread_and_no_init_turn(runtime):
    runtime.sessions.save("shared", {"thread_id": None})
    backend = runtime.backends["app"]
    backend.create = AsyncMock(wraps=backend.create)
    await runtime.start()
    await receive(runtime, Inbound("one", "first"), Inbound("two", "second"))
    backend.create.assert_awaited_once()
    thread = runtime.sessions.record("shared")["thread_id"]
    assert backend.sessions[thread].delivered == ["first", "second"]
    assert runtime.store.read_sessions()["shared"]["thread_id"] == thread
    restarted = Sessions(runtime.config, runtime.store, runtime.backends)
    assert (await restarted.ensure("shared")).thread_id == thread
    assert backend.create.await_count == 1


async def test_deleted_pointer_replaced_once_for_all_connections(runtime):
    runtime.sessions.save("shared", {"thread_id": "deleted"})
    backend = runtime.backends["app"]
    backend.create = AsyncMock(wraps=backend.create)
    await runtime.start()
    await receive(runtime, Inbound("one", "original"), Inbound("two", "next"))
    backend.create.assert_awaited_once()
    thread = runtime.sessions.record("shared")["thread_id"]
    assert thread != "deleted"
    assert backend.sessions[thread].delivered == ["original", "next"]


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionError(),
        BridgeError("codex_rejected", "provider mismatch"),
        BridgeError("session_unavailable", "unloaded, unknown reason"),
    ],
)
async def test_temporary_or_unclassified_failure_preserves_pointer(runtime, failure):
    backend = runtime.backends["app"]
    backend.load = AsyncMock(side_effect=failure)
    backend.create = AsyncMock()
    await runtime.start()
    await receive(runtime, Inbound("one", "hello"))
    assert runtime.sessions.record("shared")["thread_id"] == "chat"
    backend.create.assert_not_called()


async def test_new_session_replaces_shared_group_only_and_is_empty(runtime):
    runtime.config.telegram.bots["main"].admin = True
    runtime.config.telegram.connections["one"].allow_admin = True
    await runtime.start()
    await receive(runtime, Inbound("one", "", ("new_session", "")))
    thread = runtime.sessions.record("shared")["thread_id"]
    assert thread != "chat"
    assert runtime.sessions.record("separate")["thread_id"] == "other"
    assert not runtime.backends["app"].sessions[thread].delivered
    assert "chat" in runtime.backends["app"].sessions
    await receive(runtime, Inbound("two", "next"))
    assert runtime.backends["app"].sessions[thread].delivered == ["next"]


@pytest.mark.parametrize(
    "bot_admin,connection_admin", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_both_flags_required_and_commands_do_not_reach_model(
    runtime, bot_admin, connection_admin
):
    runtime.config.telegram.bots["main"].admin = bot_admin
    runtime.config.telegram.connections["one"].allow_admin = connection_admin
    backend = runtime.backends["app"]
    backend.create = AsyncMock(wraps=backend.create)
    await runtime.start()
    await receive(runtime, Inbound("one", "", ("new_session", "")))
    assert backend.create.await_count == int(bot_admin and connection_admin)
    assert not backend.sessions["chat"].delivered


async def test_settings_shared_persisted_validated_and_used_next_turn(runtime):
    runtime.config.telegram.bots["main"].admin = True
    runtime.config.telegram.connections["one"].allow_admin = True
    await runtime.start()
    await receive(
        runtime,
        Inbound("one", "", ("model", "other-model")),
        Inbound("one", "", ("thinking", "low")),
        Inbound("two", "hello"),
    )
    assert runtime.sessions.options("shared") == {"model": "other-model", "effort": "low"}
    assert runtime.backends["app"].sessions["chat"].options == runtime.sessions.options("shared")
    assert runtime.store.read_sessions()["shared"]["effort"] == "low"
    before = dict(runtime.sessions.record("shared"))
    await receive(
        runtime, Inbound("one", "", ("model", "invalid")), Inbound("one", "", ("thinking", "ultra"))
    )
    assert runtime.sessions.record("shared") == before
    await receive(runtime, Inbound("one", "", ("new_session", "")))
    assert runtime.sessions.options("shared") == {"model": "other-model", "effort": "low"}


async def test_ambiguous_creation_is_not_automatically_retried(runtime):
    runtime.sessions.save("shared", {"thread_id": None})
    backend = runtime.backends["app"]
    backend.create = AsyncMock(side_effect=TimeoutError())
    await runtime.start()
    await receive(runtime, Inbound("one", "first"), Inbound("two", "second"))
    backend.create.assert_awaited_once()
    assert runtime.store.read_sessions()["shared"]["creation_pending"] is True
    restarted = Sessions(runtime.config, runtime.store, runtime.backends)
    with pytest.raises(BridgeError, match="uncertain"):
        await restarted.ensure("shared")
    backend.create.assert_awaited_once()


async def test_failed_write_ahead_prevents_creation(runtime, monkeypatch):
    runtime.sessions.save("shared", {"thread_id": None})
    backend = runtime.backends["app"]
    backend.create = AsyncMock()
    monkeypatch.setattr(runtime.store, "save_sessions", lambda _: (_ for _ in ()).throw(OSError()))
    await runtime.start()
    await receive(runtime, Inbound("one", "hello"))
    backend.create.assert_not_called()
    assert runtime.sessions.record("shared") == {"thread_id": None}


async def test_independent_groups_do_not_block_each_other(runtime):
    runtime.config.telegram.connections["two"].session_id = "separate"
    entered, release = asyncio.Event(), asyncio.Event()
    transport = runtime.backends["app"].sessions["chat"]
    original = transport.deliver

    async def slow(text, options=None):
        entered.set()
        await release.wait()
        return await original(text, options)

    transport.deliver = slow
    await runtime.start()
    runtime.enqueue(Inbound("one", "slow"))
    await entered.wait()
    runtime.enqueue(Inbound("two", "fast"))
    for _ in range(20):
        await asyncio.sleep(0)
        if runtime.backends["app"].sessions["other"].delivered:
            break
    assert runtime.backends["app"].sessions["other"].delivered == ["fast"]
    release.set()
    await runtime.queue.join()


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["sessions"]["shared"].update(client="missing"),
        lambda d: d["telegram"]["connections"]["one"].update(session_id="missing"),
    ],
)
def test_config_requires_valid_session_and_backend(config, change):
    data = config.model_dump()
    change(data)
    with pytest.raises(ValidationError):
        Config.model_validate(data)


async def test_offline_before_creation_does_not_leave_uncertain_marker(runtime):
    runtime.sessions.save("shared", {"thread_id": None})
    backend = runtime.backends["app"]
    backend.connect = AsyncMock(side_effect=ConnectionError())
    backend.create = AsyncMock()
    await runtime.start()
    await receive(runtime, Inbound("one", "hello"))
    backend.create.assert_not_called()
    assert runtime.store.read_sessions()["shared"] == {"thread_id": None}


async def test_final_persistence_failure_blocks_duplicate_creation(runtime, monkeypatch):
    runtime.sessions.save("shared", {"thread_id": None})
    backend = runtime.backends["app"]
    backend.create = AsyncMock(wraps=backend.create)
    original = runtime.store.save_sessions
    count = 0

    def fail_second(records):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("disk failure")
        original(records)

    monkeypatch.setattr(runtime.store, "save_sessions", fail_second)
    await runtime.start()
    await receive(runtime, Inbound("one", "first"), Inbound("two", "second"))
    backend.create.assert_awaited_once()
    assert runtime.store.read_sessions()["shared"]["creation_pending"] is True
    assert all(not session.delivered for session in backend.sessions.values())


async def test_definite_creation_rejection_clears_pending_marker(runtime):
    from bridges.transports.rpc import RPCRejected

    runtime.sessions.save("shared", {"thread_id": None})
    backend = runtime.backends["app"]
    backend.create = AsyncMock(
        side_effect=RPCRejected({"error": {"code": -32600, "message": "Invalid workspace"}})
    )
    await runtime.start()
    await receive(runtime, Inbound("one", "hello"))
    assert runtime.store.read_sessions()["shared"] == {"thread_id": None}
