"""The independent HTTP service starts without any Codex process or credentials."""

import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path

import httpx


async def test_cli_http_start_stop_without_codex(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "instance_id": "test",
                "state_dir": str(tmp_path / "state"),
                "http": {"port": port},
                "clients": {
                    "app": {"token_env": "APP_TOKEN", "socket_path": str(tmp_path / "absent.sock")}
                },
            }
        )
    )
    env = {
        **os.environ,
        "APP_TOKEN": "x" * 32,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    for _ in range(2):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "bridges.cli",
            "serve",
            "--config",
            str(config),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        response = await client.post(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
                        if response.status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                    if proc.returncode is not None:
                        raise AssertionError((await proc.communicate())[1].decode())
                    await asyncio.sleep(0.03)
                else:
                    raise AssertionError("HTTP server did not start")
                response = await client.post(
                    f"http://127.0.0.1:{port}/mcp",
                    headers={
                        "Authorization": "Bearer " + "x" * 32,
                        "Accept": "application/json, text/event-stream",
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
                assert [t["name"] for t in response.json()["result"]["tools"]] == [
                    "get_protocol_docs"
                ]
                # Stop with a keep-alive connection still open so the server closes
                # it first. Its TIME_WAIT sockets must not block the next bind.
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
            assert not list((tmp_path / "state").rglob("binding.json"))
            assert proc.returncode == 0, stderr.decode()
            assert not stdout and b"Traceback" not in stderr
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
