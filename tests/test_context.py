from unittest.mock import AsyncMock

import pytest

from bridges.context import ContextStore
from bridges.envelope import Inbound
from bridges.transports.app_server import AppServer


@pytest.fixture
def store(tmp_path):
    value = ContextStore(tmp_path / "context.sqlite")
    try:
        yield value
    finally:
        value.close()


def test_last_reported_usage_and_unknown_limit(store):
    tracker = store.tracker("app")
    tracker.usage("chat", 80000, 100000)
    assert store.display("app", "chat") == (
        "Context usage: 80% (80,000/100,000 tokens). Last reported. "
        "0 compactions observed since tracking began."
    )
    tracker.usage("chat", 123, None)
    assert "limit unavailable" in store.display("app", "chat")
    for bad in [0, -1, False, "100", 1.5, 1 << 64]:
        tracker.usage("chat", 123, bad)
        assert "Stale:" in store.display("app", "chat")
    assert "awaiting usage update" in store.display("app", "new")


def test_backend_and_thread_isolation_compaction_replay_and_restart(tmp_path):
    path = tmp_path / "context.sqlite"
    store = ContextStore(path)
    tracker = store.tracker("app")
    tracker.usage("main", 80000, 100000)
    tracker.compaction("main", "one", completed=False)
    assert "Stale:" in store.display("app", "main")
    assert "0 compactions" in store.display("app", "main")
    tracker.usage("main", 75000, 100000)
    assert "Stale:" in store.display("app", "main")
    tracker.compaction("main", "one", completed=True)
    assert "1 compaction observed" in store.display("app", "main")
    assert "Stale:" in store.display("app", "main")
    tracker.usage("main", 10000, 100000)
    tracker.compaction("main", "one", completed=True)
    tracker.compaction("main", "one", completed=False)
    assert "10%" in store.display("app", "main")
    assert "Stale:" not in store.display("app", "main")
    tracker.compaction("child", "one", completed=True)
    assert "1 compaction observed" in store.display("app", "main")
    assert "0 compactions" in store.display("other-app", "main")
    assert "0 compactions" in store.display("app", "new")
    store.close()
    restarted = ContextStore(path)
    try:
        assert "1 compaction observed" in restarted.display("app", "main")
        assert "Stale:" in restarted.display("app", "main")
        restarted.tracker("app").compaction("main", "one", completed=True)
        assert "1 compaction observed" in restarted.display("app", "main")
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        restarted.close()


def test_disconnect_invalidates_only_its_backend(store):
    store.tracker("one").usage("chat", 1, 100)
    store.tracker("two").usage("chat", 2, 100)
    store.tracker("one").disconnect()
    assert "Stale:" in store.display("one", "chat")
    assert "Stale:" not in store.display("two", "chat")


def test_app_server_normalizes_last_usage_and_completed_compaction(config, store):
    backend = AppServer(config.clients["app"])
    backend.context_tracker = store.tracker("app")
    backend.event(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "chat",
                "turnId": "turn",
                "tokenUsage": {
                    "last": {"totalTokens": 80000},
                    "total": {"totalTokens": 9999999},
                    "modelContextWindow": 100000,
                },
            },
        }
    )
    assert "80%" in store.display("app", "chat")
    for method in ["item/started", "item/completed", "item/completed"]:
        backend.event(
            {
                "method": method,
                "params": {
                    "threadId": "chat",
                    "item": {"type": "contextCompaction", "id": "compact"},
                },
            }
        )
    assert "1 compaction observed" in store.display("app", "chat")
    backend.event({"method": "thread/compacted", "params": {"threadId": "chat"}})
    assert "1 compaction observed" in store.display("app", "chat")
    backend.disconnected()
    assert "Stale:" in store.display("app", "chat")


def test_storage_failure_does_not_break_protocol_events(store):
    store.db.execute("DROP TABLE context_compactions")
    tracker = store.tracker("app")
    tracker.compaction("chat", "one", completed=True)
    assert "tracking unavailable" in store.display("app", "chat")
    tracker.usage("chat", 100, 200)


async def test_text_send_preserves_markdown_without_context_usage(runtime):
    await runtime.start()
    runtime.context.tracker("app").usage("chat", 80000, 100000)
    runtime.context.tracker("app").usage("other", 1, 100)
    original = r"*Bold* _italic_ [link](https://example.com) `code` \!"
    result = await runtime.call(
        "telegram_send_text",
        {"connection_id": "two", "text": original},
        {"threadId": "chat"},
        "app",
    )
    assert not result.isError
    call = runtime.telegram.clients["main"].call.call_args
    assert call.args[1]["text"] == original
    assert call.args[1]["message_thread_id"] == 42
    assert call.args[1]["parse_mode"] == "MarkdownV2"


async def test_text_limit_validation_without_second_send(runtime):
    await runtime.start()
    api = runtime.telegram.clients["main"]
    api.call.reset_mock()
    result = await runtime.call(
        "telegram_send_text",
        {"connection_id": "one", "text": "a" * 4096},
        {"threadId": "chat"},
        "app",
    )
    assert result.structuredContent == {"message_id": 7}
    assert api.call.await_count == 1
    assert api.call.call_args.args[1]["text"] == "a" * 4096
    for invalid in ["", "x" * 4097, None]:
        result = await runtime.call(
            "telegram_send_text",
            {"connection_id": "one", "text": invalid},
            {"threadId": "chat"},
            "app",
        )
        assert result.isError
    assert api.call.await_count == 1


async def test_context_command_uses_no_model_or_new_thread(runtime):
    runtime.config.telegram.bots["main"].admin = True
    runtime.config.telegram.connections["one"].allow_admin = True
    backend = runtime.backends["app"]
    backend.create = AsyncMock(side_effect=AssertionError("must not create thread"))
    backend.load = AsyncMock(side_effect=AssertionError("must not load thread"))
    runtime.context.tracker("app").usage("chat", 123, 1000)
    await runtime.admin_command(Inbound("one", "/context", command=("context", "")))
    message = runtime.telegram.clients["main"].call.call_args.args[1]["text"]
    assert "12% (123/1,000 tokens)" in message
    assert "parse_mode" not in runtime.telegram.clients["main"].call.call_args.args[1]
    assert not backend.sessions["chat"].delivered
    await runtime.admin_command(Inbound("one", "/context x", command=("context", "x")))
    assert runtime.telegram.clients["main"].call.call_args.args[1]["text"] == "Usage: /context"
    runtime.config.telegram.connections["one"].allow_admin = False
    await runtime.admin_command(Inbound("one", "/context", command=("context", "")))
    assert "disabled" in runtime.telegram.clients["main"].call.call_args.args[1]["text"]
