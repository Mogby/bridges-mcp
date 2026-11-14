"""Opt-in real child-process check, no account credentials or model requests."""

import os

import pytest

from bridges.config import Codex
from bridges.errors import BridgeError
from bridges.transports.app_server import AppServerTransport


@pytest.mark.skipif(
    not os.environ.get("BRIDGES_CODEX_TEST_BINARY"),
    reason="Set BRIDGES_CODEX_TEST_BINARY for the installed App Server check",
)
async def test_installed_app_server_requires_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-be-used")
    transport = AppServerTransport(Codex(executable=os.environ["BRIDGES_CODEX_TEST_BINARY"]))
    try:
        with pytest.raises(BridgeError) as exc:
            await transport.launch("http://127.0.0.1:1/mcp", "test-bearer")
        assert exc.value.code == "subscription_login_required"
    finally:
        await transport.close()
    assert transport.proc.returncode is not None
