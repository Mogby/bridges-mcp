import asyncio
from unittest.mock import AsyncMock

import pytest

from bridges.config import Config
from bridges.runtime import Runtime
from bridges.state import InstanceState
from bridges.transports.base import Delivery, Transport, TurnState


class FakeTransport(Transport):
    def __init__(self):
        super().__init__()
        self.delivered = []
        self.update(TurnState("active", "turn-1"))

    async def launch(self, url, bearer):
        self.endpoint = url

    async def bind(self, thread_id=None):
        self.thread_id = thread_id or "chat"
        return self.thread_id

    async def snapshot(self):
        return self.state

    async def deliver(self, text):
        self.delivered.append(text)
        return Delivery("accepted", "steered")

    async def close(self):
        self.update(TurnState("disconnected"))


@pytest.fixture
def config(tmp_path):
    return Config.model_validate(
        {
            "instance_id": "test",
            "state_dir": str(tmp_path),
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
        if method == "getUpdates":
            await asyncio.Event().wait()
        return {"message_id": 7}

    api.call.side_effect = call
    runtime = Runtime(config, state, FakeTransport(), {}, {"main": api})
    try:
        yield runtime
    finally:
        await runtime.close()
        state.close()
