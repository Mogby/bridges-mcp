import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bridges.config import Config
from bridges.errors import BridgeError
from bridges.state import InstanceState


def test_application_launch_settings_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"instance_id": "test", "codex": {"executable": "codex"}})


async def test_bridges_starts_without_application(runtime):
    backend = runtime.backends["app"]
    backend.session = AsyncMock(side_effect=BridgeError("session_unavailable", "offline"))
    await runtime.start()
    assert runtime.ready and runtime.telegram.tasks
    backend.session.assert_not_called()
    assert runtime.store.read_focus() == {}
    assert not (runtime.store.directory / "binding.json").exists()
    result = await runtime.call("focus", {"connection_id": "one"}, {"threadId": "chat"}, "app")
    assert result.isError and runtime.store.read_focus() == {}


@pytest.mark.parametrize(
    "connections",
    [
        {"one": ["a", "b"]},
        {"one": {"client_id": "app"}},
        {"one": {"client_id": "", "thread_id": "x"}},
    ],
)
def test_invalid_focus_rejected(tmp_path, connections):
    state = InstanceState(tmp_path, "test")
    state.acquire()
    try:
        state.focus_path.write_text(json.dumps({"instance_id": "test", "connections": connections}))
        with pytest.raises(BridgeError):
            state.read_focus()
    finally:
        state.close()


def test_client_tokens_unique_and_required(config, monkeypatch):
    with pytest.raises(BridgeError):
        config.client_tokens()
    monkeypatch.setenv("APP_TOKEN", "a" * 32)
    assert config.client_tokens() == {"app": "a" * 32}
    config.clients["other"] = config.clients["app"]
    with pytest.raises(BridgeError):
        config.client_tokens()
