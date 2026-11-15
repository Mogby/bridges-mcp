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


async def test_bridges_starts_without_daemon(runtime):
    backend = runtime.backends["app"]
    backend.load = AsyncMock(side_effect=BridgeError("session_unavailable", "offline"))
    await runtime.start()
    assert runtime.ready and runtime.telegram.tasks
    backend.load.assert_not_called()
    assert runtime.store.read_sessions()["shared"]["thread_id"] == "chat"


@pytest.mark.parametrize(
    "sessions",
    [
        {"shared": ["a", "b"]},
        {"shared": {"model": "test"}},
        {"shared": {"thread_id": ""}},
    ],
)
def test_invalid_session_state_rejected(tmp_path, sessions):
    state = InstanceState(tmp_path, "test")
    state.acquire()
    try:
        state.sessions_path.write_text(json.dumps({"instance_id": "test", "sessions": sessions}))
        with pytest.raises(BridgeError):
            state.read_sessions()
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
