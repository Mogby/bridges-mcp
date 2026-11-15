import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bridges.admin import COMMANDS, command_from
from bridges.config import Bot, Connection
from bridges.errors import BridgeError


def test_admin_configuration_is_strict_and_opt_in():
    assert not Bot(token_env="TOKEN").admin
    assert not Connection(bot="main", chat_id="1", session_id="shared").allow_admin
    for factory, values in [
        (Bot, {"token_env": "TOKEN", "admin": "true"}),
        (Connection, {"bot": "main", "chat_id": "1", "session_id": "shared", "allow_admin": 1}),
    ]:
        with pytest.raises(ValidationError):
            factory(**values)


@pytest.mark.parametrize(
    "text,entities,expected",
    [
        ("/new_session", [{"type": "bot_command", "offset": 0}], ("new_session", "")),
        ("/model@MyBot gpt-test", [{"type": "bot_command", "offset": 0}], ("model", "gpt-test")),
        ("/thinking high", [{"type": "bot_command", "offset": 0}], ("thinking", "high")),
        ("/model@OtherBot x", [{"type": "bot_command", "offset": 0}], None),
        ("/model x", [], None),
        ("quoted /model x", [{"type": "bot_command", "offset": 7}], None),
        ("/new", [{"type": "bot_command", "offset": 0}], None),
    ],
)
def test_only_real_leading_commands_are_recognized(text, entities, expected):
    assert command_from({"text": text, "entities": entities}, "mybot") == expected


async def test_admin_bot_registers_commands_and_handles_addressed_commands(config):
    from bridges.envelope import Handles
    from bridges.telegram import TelegramBridge

    config.telegram.bots["main"].admin = True
    api, envelopes, ready = AsyncMock(), [], asyncio.Event()
    polls = 0

    async def call(method, data=None):
        nonlocal polls
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "getMe":
            return {"username": "MyBot"}
        if method == "setMyCommands":
            assert data == {"commands": COMMANDS}
            return True
        polls += 1
        if polls == 1:
            return [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 1, "type": "private"},
                        "text": "/model@MyBot",
                        "entities": [{"type": "bot_command", "offset": 0}],
                    },
                }
            ]
        ready.set()
        await asyncio.Event().wait()

    api.call.side_effect = call
    bridge = TelegramBridge(config, {}, Handles(), envelopes.append, {"main": api})
    try:
        await bridge.start()
        await asyncio.wait_for(ready.wait(), 1)
        assert envelopes[0].command == ("model", "")
        assert [call.args[0] for call in api.call.call_args_list].count("setMyCommands") == 1
    finally:
        await bridge.close()


async def test_startup_configures_permitted_chats_and_private_menu_only(config):
    from bridges.config import Connection
    from bridges.envelope import Handles
    from bridges.telegram import TelegramBridge

    config.telegram.connections["one"].allow_admin = True
    config.telegram.connections["two"].allow_admin = True
    config.telegram.connections["duplicate"] = Connection(
        bot="main", chat_id="2", session_id="shared", message_thread_id=43, allow_admin=True
    )
    config.telegram.connections["denied"] = Connection(bot="main", chat_id="3", session_id="shared")
    api = AsyncMock()

    async def call(method, data=None):
        if method == "getMe":
            return {"username": "MyBot"}
        if method == "getChat":
            return {"type": "private" if data["chat_id"] == "1" else "supergroup"}
        return True

    api.call.side_effect = call
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    await bridge.register_admin_commands("main")
    calls = api.call.call_args_list
    scoped = [
        c.args[1]["scope"]["chat_id"]
        for c in calls
        if c.args[0] == "setMyCommands" and "scope" in c.args[1]
    ]
    assert scoped == ["1", "2"]
    menus = [c.args[1] for c in calls if c.args[0] == "setChatMenuButton"]
    assert menus == [{"chat_id": "1", "menu_button": {"type": "commands"}}]


async def test_failed_chat_menu_does_not_block_remaining_connections(config):
    from bridges.envelope import Handles
    from bridges.telegram import TelegramBridge

    for connection in config.telegram.connections.values():
        connection.allow_admin = True
    api = AsyncMock()

    async def call(method, data=None):
        if method == "getMe":
            return {"username": "MyBot"}
        if method == "setMyCommands" and data.get("scope", {}).get("chat_id") == "1":
            raise BridgeError("telegram_unavailable", "Unavailable chat")
        if method == "getChat":
            return {"type": "private"}
        return True

    api.call.side_effect = call
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    await bridge.register_admin_commands("main")
    api.call.assert_any_await(
        "setChatMenuButton", {"chat_id": "2", "menu_button": {"type": "commands"}}
    )
