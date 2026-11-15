"""The test owns this real App Server; Bridges only attaches to its socket."""

import asyncio
import os

import pytest

from bridges.config import Client
from bridges.errors import BridgeError
from bridges.transports.app_server import AppServer


@pytest.mark.skipif(
    not os.environ.get("BRIDGES_CODEX_TEST_BINARY"), reason="Set BRIDGES_CODEX_TEST_BINARY"
)
async def test_installed_app_server_connection_does_not_own_process(tmp_path, socket_path):
    (tmp_path / "home").mkdir()
    path = socket_path
    proc = await asyncio.create_subprocess_exec(
        os.environ["BRIDGES_CODEX_TEST_BINARY"],
        "app-server",
        "--listen",
        "unix://" + path,
        env={**os.environ, "CODEX_HOME": str(tmp_path / "home")},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    backend = AppServer(Client(token_env="TOKEN", socket_path=path))
    try:
        for _ in range(100):
            if os.path.exists(path):
                break
            await asyncio.sleep(0.05)
        if proc.returncode is not None:
            raise AssertionError((await proc.communicate())[1].decode())
        await backend.connect()
        with pytest.raises(BridgeError):
            await backend.session("missing")
        # Create an empty session: no synthetic user message or model turn.
        created = await backend.create(str(tmp_path), {})
        thread = created["thread_id"]
        assert (await backend.session(thread)).state.status == "idle"
        await backend.load(thread, str(tmp_path), {})
        models = await backend.models()
        assert models
        import uuid

        missing = str(uuid.uuid4())
        with pytest.raises(BridgeError) as absent:
            await backend.load(missing, str(tmp_path), {})
        assert absent.value.code == "thread_missing"
        await backend.close()
        assert proc.returncode is None
    finally:
        await backend.close()
        if proc.returncode is None:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 5)
