import asyncio
from unittest.mock import AsyncMock

import pytest

from bridges.config import Config
from bridges.errors import BridgeError
from bridges.runtime import Runtime
from bridges.state import InstanceState
from bridges.transports.base import Delivery, Transport, TurnState


class FakeTransport(Transport):
    def __init__(self, thread_id="chat"):
        super().__init__()
        self.thread_id = thread_id
        self.delivered = []
        self.update(TurnState("active", "turn-1"))

    async def snapshot(self):
        return self.state

    async def deliver(self, text):
        self.delivered.append(text)
        return Delivery("accepted", "steered", self.state.turn_id)


class FakeBackend:
    def __init__(self):
        self.sessions = {name: FakeTransport(name) for name in ["chat", "other"]}

    async def session(self, thread_id):
        if thread_id not in self.sessions:
            raise BridgeError("session_unavailable", "Unknown session")
        return self.sessions[thread_id]

    async def close(self):
        for session in self.sessions.values():
            session.update(TurnState("disconnected"))


@pytest.fixture
def config(tmp_path):
    return Config.model_validate(
        {
            "instance_id": "test",
            "state_dir": str(tmp_path),
            "clients": {"app": {"token_env": "APP_TOKEN", "socket_path": "/tmp/app.sock"}},
            "telegram": {
                "bots": {"main": {"token_env": "TEST_TOKEN"}},
                "connections": {
                    "one": {"bot": "main", "chat_id": "1"},
                    "two": {"bot": "main", "chat_id": "2", "message_thread_id": 42},
                },
            },
        }
    )


@pytest.fixture
async def runtime(config):
    state = InstanceState(config.state_path, config.instance_id)
    state.acquire()
    api = AsyncMock()

    async def call(method, *args, **kwargs):
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "getMe":
            return {"username": "TestBot"}
        if method == "setMyCommands":
            return True
        if method == "getUpdates":
            await asyncio.Event().wait()
        return {"message_id": 7}

    api.call.side_effect = call
    runtime = Runtime(config, state, {}, {"main": api}, {"app": FakeBackend()})
    try:
        yield runtime
    finally:
        await runtime.close()
        state.close()


@pytest.fixture
def socket_path():
    import tempfile
    from pathlib import Path

    # macOS Unix sockets cannot use pytest's long default temp paths.
    with tempfile.TemporaryDirectory(prefix="bm-", dir="/tmp") as directory:
        yield str(Path(directory) / "app.sock")
