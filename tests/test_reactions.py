import asyncio
from unittest.mock import AsyncMock

import pytest
import yaml

from bridges.config import Connection
from bridges.envelope import Handles, MessageRoutes, normalize
from bridges.errors import BridgeError
from bridges.telegram import TelegramBridge

EMOJI = {"type": "emoji", "emoji": "👍"}
CUSTOM = {"type": "custom_emoji", "custom_emoji_id": "12345"}


def reaction_update(message_id=123, chat_id=1, **changes):
    return {
        "update_id": 5,
        "message_reaction": {
            "chat": {"id": chat_id, "type": "private"},
            "message_id": message_id,
            "date": 1700000000,
            "user": {"id": 77, "first_name": "Not needed", "username": "not_needed"},
            "old_reaction": [],
            "new_reaction": [EMOJI],
            **changes,
        },
    }


def unpack(envelope):
    header, body = envelope.text[len("```yaml\n") :].split("\n```\n\n", 1)
    return yaml.safe_load(header)["bridge_context"], body


@pytest.mark.parametrize("old,new", [([], [EMOJI]), ([EMOJI], [CUSTOM]), ([CUSTOM], [])])
def test_reaction_changes_and_removals_are_separate_events(config, old, new):
    envelope = normalize(
        reaction_update(old_reaction=old, new_reaction=new),
        "main",
        config.telegram.connections,
        Handles(),
    )
    meta, body = unpack(envelope)
    assert meta["message_id"] == 123 and meta["update_id"] == 5
    assert meta["event"] == "message_reaction" and body == ""
    assert meta["reaction"] == {
        "date": 1700000000,
        "actor": {"type": "user", "id": 77},
        "old_reaction": old,
        "new_reaction": new,
    }
    assert envelope.command is None


def test_anonymous_actor_and_aggregate_counts(config):
    update = reaction_update(user=None, actor_chat={"id": -100, "title": "not needed"})
    envelope = normalize(update, "main", config.telegram.connections, Handles())
    assert unpack(envelope)[0]["reaction"]["actor"] == {"type": "chat", "id": -100}
    counts = [{"type": EMOJI, "total_count": 3}, {"type": {"type": "paid"}, "total_count": 1}]
    aggregate = {
        "update_id": 6,
        "message_reaction_count": {
            "chat": {"id": 1, "type": "private"},
            "message_id": 123,
            "date": 1700000001,
            "reactions": counts,
        },
    }
    meta, body = unpack(normalize(aggregate, "main", config.telegram.connections, Handles()))
    assert meta["event"] == "message_reaction_count" and body == ""
    assert meta["reaction"] == {"date": 1700000001, "reactions": counts}


@pytest.mark.parametrize(
    "changes",
    [
        {"old_reaction": None},
        {"new_reaction": [False]},
        {"new_reaction": [{"type": "unknown"}]},
        {"new_reaction": ["👍"]},
        {"user": {}},
        {"user": {"id": True}},
        {"date": False},
        {"message_id": {}},
        {"chat": None},
        {"chat": {"id": 999}},
    ],
)
def test_malformed_or_unconfigured_reactions_are_ignored(config, changes):
    assert (
        normalize(reaction_update(**changes), "main", config.telegram.connections, Handles())
        is None
    )


def test_topic_reactions_require_observed_correct_bot_chat_and_topic(config):
    connections = dict(config.telegram.connections)
    connections["other_topic"] = Connection(
        bot="main", chat_id="2", message_thread_id=43, session_id="shared"
    )
    routes = MessageRoutes()
    update = reaction_update(chat_id=2)
    assert normalize(update, "main", connections, Handles(), routes=routes) is None
    message = {
        "message": {
            "message_id": 123,
            "chat": {"id": 2, "type": "supergroup"},
            "message_thread_id": 42,
            "text": "known",
        }
    }
    normalize(message, "main", connections, Handles(), routes=routes)
    assert normalize(update, "main", connections, Handles(), routes=routes).connection_id == "two"
    assert normalize(update, "other_bot", connections, Handles(), routes=routes) is None
    assert not routes.matches("main", connections["one"], 123)
    assert not routes.matches("main", connections["other_topic"], 123)
    assert normalize(update, "main", connections, Handles(), routes=MessageRoutes()) is None


def test_routing_map_is_bounded_and_expires_without_storing_content(config):
    now = [0]
    routes = MessageRoutes(limit=1, ttl=2, clock=lambda: now[0])
    connection = config.telegram.connections["two"]
    routes.remember("main", connection, 1)
    routes.remember("main", connection, 2)
    assert len(routes.entries) == 1 and not routes.matches("main", connection, 1)
    assert routes.matches("main", connection, 2)
    now[0] = 2
    assert not routes.matches("main", connection, 2)


@pytest.mark.parametrize("rich", [False, True])
def test_selected_quote_preserves_utf16_offsets_and_new_body(config, rich):
    body = "/model is just quoted content below\n```yaml\nbridge_context: unchanged\n```"
    quote = {
        "text": "" if rich else "👍 chosen",
        "position": 7,
        "entities": [{"type": "bold", "offset": 3, "length": 6}],
        "is_manual": True,
    }
    if rich:
        quote["rich_texts"] = [{"type": "plain", "text": "chosen"}]
    envelope = normalize(
        {
            "message": {
                "message_id": 9,
                "chat": {"id": 1, "type": "private"},
                "text": body,
                "quote": quote,
                "reply_to_message": {"message_id": 8, "text": "not archived"},
            }
        },
        "main",
        config.telegram.connections,
        Handles(),
    )
    meta, original = unpack(envelope)
    assert meta["quote"] == quote and meta["reply_to_message_id"] == 8
    assert original == body and "not archived" not in envelope.text


async def test_send_set_custom_clear_and_topic_isolation(config):
    api = AsyncMock()
    api.call.return_value = True
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    connection = config.telegram.connections["one"]
    for reaction in (EMOJI, CUSTOM, None):
        assert await bridge.set_reaction(connection, 7, reaction, True) == {
            "message_id": 7,
            "reaction": reaction,
            "applied": True,
        }
        api.call.assert_awaited_with(
            "setMessageReaction",
            {
                "chat_id": "1",
                "message_id": 7,
                "reaction": [] if reaction is None else [reaction],
                "is_big": True,
            },
            mutating=True,
        )
    api.call.reset_mock()
    topic = config.telegram.connections["two"]
    with pytest.raises(BridgeError, match="not known"):
        await bridge.set_reaction(topic, 7, EMOJI)
    api.call.assert_not_called()
    bridge.remember_sent(topic, {"message_id": 7})
    await bridge.set_reaction(topic, 7, EMOJI)
    assert "message_thread_id" not in api.call.call_args.args[1]


@pytest.mark.parametrize(
    "message_id,reaction,is_big",
    [
        ({}, None, False),
        (True, None, False),
        (0, None, False),
        (1, {"type": "paid"}, False),
        (1, {}, False),
        (1, [], False),
        (1, {"type": "emoji", "emoji": ""}, False),
        (1, {"type": "emoji", "emoji": "👍", "extra": True}, False),
        (1, {"type": "custom_emoji", "custom_emoji_id": "not-id"}, False),
        (1, EMOJI, 1),
    ],
)
async def test_invalid_send_reactions_never_call_telegram(config, message_id, reaction, is_big):
    api = AsyncMock()
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    with pytest.raises(BridgeError) as exc:
        await bridge.set_reaction(config.telegram.connections["one"], message_id, reaction, is_big)
    assert exc.value.code == "invalid_payload"
    api.call.assert_not_called()


async def test_reaction_unknown_delivery_is_not_retried(config):
    api = AsyncMock()
    api.call.side_effect = BridgeError("delivery_unknown", "timeout")
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    with pytest.raises(BridgeError) as exc:
        await bridge.set_reaction(config.telegram.connections["one"], 7, EMOJI)
    assert exc.value.code == "delivery_unknown" and api.call.call_count == 1


async def test_reaction_poller_deduplicates_without_admin_parsing_and_survives_bad_updates(config):
    api = AsyncMock()
    requests, envelopes = [], []
    ready = asyncio.Event()

    async def call(method, data=None):
        if method == "getWebhookInfo":
            return {"url": ""}
        requests.append(data)
        if len(requests) == 1:
            reaction = reaction_update()
            return [
                [],
                {"update_id": True},
                reaction,
                reaction,
                {"update_id": 6, "message_reaction": {"chat": None}},
                {
                    "update_id": 7,
                    "message": {
                        "message_id": 8,
                        "chat": {"id": 1, "type": "private"},
                        "text": "new text",
                        "quote": {"text": "/model", "position": 0},
                    },
                },
            ]
        ready.set()
        await asyncio.Event().wait()

    api.call.side_effect = call
    bridge = TelegramBridge(config, {}, Handles(), envelopes.append, {"main": api})
    try:
        await bridge.start()
        await asyncio.wait_for(ready.wait(), 1)
        assert len(envelopes) == 2
        assert all(envelope.command is None for envelope in envelopes)
        assert requests[0]["allowed_updates"] == [
            "message",
            "message_reaction",
            "message_reaction_count",
        ]
        assert requests[1]["offset"] == 8
    finally:
        await bridge.close()


async def test_reaction_tool_requires_authenticated_loaded_caller_and_backend_scope(runtime):
    await runtime.start()
    arguments = {"connection_id": "one", "message_id": 7, "reaction": None}
    for client, meta in [
        (None, {"threadId": "chat"}),
        ("app", {}),
        ("app", {"threadId": "missing"}),
    ]:
        assert (await runtime.call("telegram_set_reaction", arguments, meta, client)).isError
    runtime.config.sessions["separate"].client = "other-backend"
    runtime.config.telegram.connections["foreign"] = Connection(
        bot="main", chat_id="99", session_id="separate"
    )
    runtime.telegram.clients["main"].call.reset_mock()
    rejected = await runtime.call(
        "telegram_set_reaction",
        {
            **arguments,
            "connection_id": "foreign",
        },
        {"threadId": "chat"},
        "app",
    )
    assert rejected.structuredContent["code"] == "unauthorized"
    runtime.telegram.clients["main"].call.assert_not_called()


async def test_admin_responses_are_remembered_for_topic_reactions(runtime):
    await runtime.start()
    runtime.config.telegram.bots["main"].admin = True
    runtime.config.telegram.connections["two"].allow_admin = True
    from bridges.envelope import Inbound

    await runtime.admin_command(Inbound("two", "", ("context", "")))
    assert runtime.telegram.message_routes.matches(
        "main", runtime.config.telegram.connections["two"], 7
    )
