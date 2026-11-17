import asyncio
from unittest.mock import AsyncMock

import pytest
import yaml

from bridges.envelope import Handles, normalize, sender_metadata
from bridges.telegram import BotMessageGate, TelegramBridge


def message(sender_id=77, is_bot=False, chat_type="group", **fields):
    return {
        "message_id": 123,
        "chat": {"id": 1, "type": chat_type},
        "from": {"id": sender_id, "is_bot": is_bot, "username": "ActualSender"},
        "text": "hello",
        **fields,
    }


def test_sender_metadata_preserves_actual_identity_and_anonymous_chat(config):
    original = message(forward_origin={"sender_user": {"id": 999, "is_bot": True}})
    assert sender_metadata(original) == {
        "type": "user",
        "id": 77,
        "is_bot": False,
        "username": "ActualSender",
    }
    original["sender_chat"] = {"id": -100, "username": "ActualChat"}
    original["from"] = {"id": 1087968824, "is_bot": True, "username": "GroupAnonymousBot"}
    envelope = normalize({"message": original}, "main", config.telegram.connections, Handles())
    header = envelope.text[len("```yaml\n") :].split("\n```\n\n", 1)[0]
    assert yaml.safe_load(header)["bridge_context"]["sender"] == {
        "type": "chat",
        "id": -100,
        "username": "ActualChat",
    }


@pytest.mark.parametrize("sender", [None, {}, {"id": True}, {"id": -1}, {"id": 0}])
def test_unknown_sender_identity_is_not_invented(sender):
    assert sender_metadata(message(**{"from": sender})) is None


def test_bot_exchange_is_finite_until_human_reset_and_does_not_extend_window():
    now = [0]
    gate = BotMessageGate(clock=lambda: now[0])
    bot = message(88, True)
    human = message()
    assert not gate.accept("group", bot)
    assert gate.accept("group", human)
    assert all(gate.accept("group", bot) for _ in range(4))
    assert not any(gate.accept("group", bot) for _ in range(20))
    # Different bot senders cannot replenish this connection's finite budget.
    assert not gate.accept("group", message(99, True))
    assert gate.accept("group", human)
    now[0] = 299
    assert gate.accept("group", bot)
    now[0] = 300
    assert not gate.accept("group", bot)
    assert gate.accept("group", human)
    assert gate.accept("group", bot)


def test_unknown_and_anonymous_senders_cannot_open_or_replenish_gate():
    gate = BotMessageGate()
    unknowns = [
        message(**{"from": None}),
        message(**{"from": {"id": 77}}),
        message(sender_chat={"id": -100}),
    ]
    for unknown in unknowns:
        assert gate.accept("group", unknown)
        assert not gate.accept("group", message(88, True))
    gate.accept("group", message())
    for _ in range(4):
        assert gate.accept("group", message(88, True))
    for unknown in unknowns:
        gate.accept("group", unknown)
    assert not gate.accept("group", message(88, True))


def test_gate_is_route_scoped_bounded_and_closed_after_restart():
    gate = BotMessageGate(limit=1)
    gate.accept("first", message())
    assert not gate.accept("other", message(88, True))
    gate.accept("other", message())
    assert len(gate.entries) == 1 and not gate.accept("first", message(88, True))
    assert not BotMessageGate().accept("other", message(88, True))


def test_own_bot_is_ignored_and_other_private_messages_are_unchanged():
    gate = BotMessageGate()
    for chat_type in ("group", "supergroup", "private"):
        assert not gate.accept("route", message(123, True, chat_type), own_bot_id=123)
        assert gate.accept("route", message(77, False, chat_type), own_bot_id=123)
    assert gate.accept("route", message(88, True, "private"), own_bot_id=123)


async def test_poller_gates_bot_messages_and_never_executes_their_admin_commands(config):
    ready = asyncio.Event()
    api = AsyncMock()
    envelopes, batches = [], []
    bot_command = message(
        88, True, text="/new_session", entities=[{"type": "bot_command", "offset": 0, "length": 12}]
    )
    human_command = message(
        text="/context", entities=[{"type": "bot_command", "offset": 0, "length": 8}]
    )

    async def call(method, data=None):
        if method == "getWebhookInfo":
            return {"url": ""}
        batches.append(data)
        if len(batches) == 1:
            messages = [
                bot_command,
                message(),
                *([bot_command] * 6),
                message(123, True),
                human_command,
                bot_command,
            ]
            return [{"update_id": index, "message": value} for index, value in enumerate(messages)]
        ready.set()
        await asyncio.Event().wait()

    api.call.side_effect = call
    # admin:false still knows its own bot ID from the configured credential.
    bridge = TelegramBridge(
        config, {"main": "123:secret"}, Handles(), envelopes.append, {"main": api}
    )
    try:
        await bridge.start()
        await asyncio.wait_for(ready.wait(), 1)
        assert len(envelopes) == 7  # human, four bots, human admin, one bot
        assert envelopes[-2].command == ("context", "")
        assert all(
            envelope.command is None
            for index, envelope in enumerate(envelopes)
            if index != len(envelopes) - 2
        )
        assert all(envelope.connection_id == "one" for envelope in envelopes)
        assert not any(c.args[0] == "getMe" for c in api.call.call_args_list)
    finally:
        await bridge.close()


async def test_anonymous_admin_proxy_command_is_data_and_does_not_open_bot_gate(config):
    ready = asyncio.Event()
    api = AsyncMock()
    envelopes, batches = [], []
    anonymous = message(
        sender_id=1087968824,
        is_bot=True,
        sender_chat={"id": -100},
        text="/context",
        entities=[{"type": "bot_command", "offset": 0, "length": 8}],
    )

    async def call(method, data=None):
        if method == "getWebhookInfo":
            return {"url": ""}
        batches.append(data)
        if len(batches) == 1:
            return [
                {"update_id": 1, "message": anonymous},
                {"update_id": 2, "message": message(88, True)},
            ]
        ready.set()
        await asyncio.Event().wait()

    api.call.side_effect = call
    bridge = TelegramBridge(config, {}, Handles(), envelopes.append, {"main": api})
    try:
        await bridge.start()
        await asyncio.wait_for(ready.wait(), 1)
        assert len(envelopes) == 1 and envelopes[0].command is None
        assert envelopes[0].text.endswith("/context")
        assert not bridge.bot_message_gate.entries
    finally:
        await bridge.close()
