import asyncio
from unittest.mock import AsyncMock

import pytest

from bridges.config import Config
from bridges.errors import BridgeError
from bridges.runtime import Runtime
from bridges.state import State
from bridges.transports.base import Delivery, ModelInfo, Transport, TurnState


class FakeTransport(Transport):
    def __init__(self, thread_id="chat"):
        super().__init__()
        self.thread_id = thread_id
        self.delivered = []
        self.update(TurnState("active", "turn-1"))

    async def snapshot(self):
        return self.state

    async def deliver(self, text, options=None):
        self.delivered.append(text)
        self.options = options or {}
        if self.state.status == "idle":
            self.update(TurnState("active", "turn-1"))
        return Delivery("accepted", "steered", self.state.turn_id)


class FakeBackend:
    def __init__(self):
        self.sessions = {name: FakeTransport(name) for name in ["chat", "other"]}

    async def connect(self):
        pass

    async def session(self, thread_id):
        if thread_id not in self.sessions:
            raise BridgeError("session_unavailable", "Unknown session")
        return self.sessions[thread_id]

    async def load(self, thread_id, cwd, options):
        if thread_id not in self.sessions:
            raise BridgeError("thread_missing", "Deleted thread")
        return self.sessions[thread_id]

    async def create(self, cwd, options):
        name = "new-" + str(len(self.sessions))
        self.sessions[name] = FakeTransport(name)
        self.sessions[name].update(TurnState("idle"))
        return {
            "thread_id": name,
            "model": options.get("model", "test"),
            "effort": options.get("effort", "medium"),
        }

    async def models(self):
        return [
            ModelInfo(id=model, efforts=("low", "medium", "high"), default_effort="medium")
            for model in ("test", "other-model")
        ]

    async def close(self):
        for session in self.sessions.values():
            session.update(TurnState("disconnected"))


@pytest.fixture
def config(tmp_path):
    return Config.model_validate(
        {
            "state_dir": str(tmp_path),
            "clients": {"app": {"token_env": "APP_TOKEN", "socket_path": "/tmp/app.sock"}},
            "sessions": {
                "shared": {"client": "app", "cwd": str(tmp_path)},
                "separate": {"client": "app", "cwd": str(tmp_path)},
            },
            "telegram": {
                "bots": {"main": {"token_env": "TEST_TOKEN"}},
                "connections": {
                    "one": {"bot": "main", "chat_id": "1", "session_id": "shared"},
                    "two": {
                        "bot": "main",
                        "chat_id": "2",
                        "message_thread_id": 42,
                        "session_id": "shared",
                    },
                },
            },
        }
    )


@pytest.fixture
async def runtime(config):
    state = State(config.state_path)
    state.acquire()
    state.save_sessions(
        {
            "shared": {"thread_id": "chat", "model": "test", "effort": "medium"},
            "separate": {"thread_id": "other", "model": "test", "effort": "medium"},
        }
    )
    api = AsyncMock()

    async def call(method, *args, **kwargs):
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "getMe":
            return {"username": "TestBot"}
        if method == "setMyCommands":
            return True
        if method == "setMessageReaction":
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
