import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bridges.admin import COMMANDS, ApplicationControl, command_from
from bridges.config import Bot, Connection
from bridges.envelope import Inbound
from bridges.errors import BridgeError
from bridges.transports.app_server import AppServerSession
from bridges.transports.base import TurnState


def application_control():
    spec = importlib.util.spec_from_file_location(
        "example_control", Path(__file__).parents[1] / "examples/application_control.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SessionControl


def test_admin_configuration_is_strict_and_opt_in():
    assert not Bot(token_env="TOKEN").admin
    assert not Connection(bot="main", chat_id="1").allow_admin
    for factory, values in [
        (Bot, {"token_env": "TOKEN", "admin": "true"}),
        (Connection, {"bot": "main", "chat_id": "1", "allow_admin": 1}),
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


@pytest.mark.parametrize(
    "bot_admin,allow_admin", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_commands_require_both_flags_and_never_reach_model(runtime, bot_admin, allow_admin):
    await runtime.start()
    await runtime.focus("one", "app", "chat")
    runtime.config.telegram.bots["main"].admin = bot_admin
    runtime.config.telegram.connections["one"].allow_admin = allow_admin
    control = AsyncMock()
    control.request.return_value = {"thread_id": "chat", "message": "Model: gpt-test"}
    runtime.controls["app"] = control
    runtime.enqueue(Inbound("one", "untrusted rendered text", ("model", "gpt-test")))
    await runtime.queue.join()
    assert control.request.await_count == int(bot_admin and allow_admin)
    assert runtime.backends["app"].sessions["chat"].delivered == []
    replies = [
        call
        for call in runtime.telegram.clients["main"].call.call_args_list
        if call.args[0] == "sendMessage"
    ]
    assert len(replies) == 1
    reply = replies[0]
    assert "parse_mode" not in reply.args[1]


async def test_new_session_moves_only_requested_focus_and_preserves_old_conversation(runtime):
    await runtime.start()
    for connection in ("one", "two"):
        await runtime.focus(connection, "app", "chat")
    runtime.config.telegram.bots["main"].admin = True
    runtime.config.telegram.connections["one"].allow_admin = True
    control = AsyncMock()
    control.request.return_value = {"thread_id": "other", "message": "New session ready."}
    runtime.controls["app"] = control
    runtime.enqueue(Inbound("one", "", ("new_session", "")))
    runtime.enqueue(Inbound("one", "after"))
    await runtime.queue.join()
    assert runtime.focuses["one"]["thread_id"] == "other"
    assert runtime.focuses["two"]["thread_id"] == "chat"
    assert runtime.store.read_focus() == runtime.focuses
    assert runtime.backends["app"].sessions["other"].delivered == ["after"]
    assert "chat" in runtime.backends["app"].sessions


async def test_unavailable_control_and_invalid_new_session_do_not_change_focus(runtime):
    await runtime.start()
    await runtime.focus("one", "app", "chat")
    runtime.config.telegram.bots["main"].admin = True
    runtime.config.telegram.connections["one"].allow_admin = True
    for command in [("model", ""), ("new_session", "unexpected argument")]:
        runtime.enqueue(Inbound("one", "", command))
        await runtime.queue.join()
        assert runtime.focuses["one"]["thread_id"] == "chat"
    assert not runtime.backends["app"].sessions["chat"].delivered


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


async def test_application_control_round_trip_persistence_and_new_session(tmp_path, socket_path):
    model = {
        "id": "test",
        "model": "test",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}],
    }

    async def call(method, params):
        if method == "model/list":
            return {"data": [model], "nextCursor": None}
        if method == "thread/start":
            return {"thread": {"id": "new"}, "model": "test", "reasoningEffort": "medium"}
        raise AssertionError(method)

    state = tmp_path / "session.json"
    owner = application_control()(call, state, {"cwd": str(tmp_path)})
    owner.loaded({"thread": {"id": "old"}, "model": "test", "reasoningEffort": "medium"})
    await owner.start(socket_path)
    control = ApplicationControl(socket_path)
    try:
        assert (
            "Current model: test" in (await control.request("one", "old", "model", ""))["message"]
        )
        await control.request("one", "old", "thinking", "low")
        assert await control.turn_settings("old") == {"model": "test", "effort": "low"}
        for command, argument in [("model", "nonexistent"), ("thinking", "ultra")]:
            with pytest.raises(BridgeError, match="Unknown model|Unsupported thinking"):
                await control.request("one", "old", command, argument)
        assert json.loads(state.read_text())["sessions"]["old"]["effort"] == "low"
        restarted = application_control()(call, state, {})
        restarted.loaded({"thread": {"id": "old"}, "model": "test", "reasoningEffort": "medium"})
        assert restarted.sessions["old"]["effort"] == "low"
        assert (await control.request("one", "old", "new_session", ""))["thread_id"] == "new"
        assert json.loads(state.read_text())["thread_id"] == "new"
        assert owner.sessions["old"]["effort"] == "low"
        assert await control.turn_settings("new") == {"model": "test", "effort": "medium"}
    finally:
        await owner.close()


@pytest.mark.parametrize(
    "response", [b"not json\n", b"{}\n", b'{"thread_id":"wrong","message":"ok"}\n']
)
async def test_invalid_control_response_is_not_retried(socket_path, response):
    calls = []

    async def handle(reader, writer):
        calls.append(await reader.readline())
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_unix_server(handle, path=socket_path):
        with pytest.raises(BridgeError) as exc:
            await ApplicationControl(socket_path).request("one", "chat", "model", "test")
        assert exc.value.code == "admin_outcome_unknown"
        assert len(calls) == 1


async def test_owned_settings_apply_on_start_but_not_steer():
    control = AsyncMock()
    control.turn_settings.return_value = {"model": "test", "effort": "low"}
    session = AppServerSession(AsyncMock(), "chat", control)
    session.snapshot = AsyncMock(return_value=TurnState("idle"))
    session.rpc.request.return_value = {"turn": {"id": "new"}}
    assert (await session.deliver("hello")).outcome == "accepted"
    assert session.rpc.request.call_args.args[1]["model"] == "test"
    assert session.rpc.request.call_args.args[1]["effort"] == "low"
    session.snapshot.return_value = TurnState("active", "new")
    await session.deliver("more")
    assert "model" not in session.rpc.request.call_args.args[1]
    assert control.turn_settings.await_count == 1


async def test_startup_configures_permitted_chats_and_private_menu_only(config):
    from bridges.config import Connection
    from bridges.envelope import Handles
    from bridges.telegram import TelegramBridge

    config.telegram.connections["one"].allow_admin = True
    config.telegram.connections["two"].allow_admin = True
    config.telegram.connections["duplicate"] = Connection(
        bot="main", chat_id="2", message_thread_id=43, allow_admin=True
    )
    config.telegram.connections["denied"] = Connection(bot="main", chat_id="3")
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
