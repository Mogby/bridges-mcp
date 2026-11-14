"""Run the actual CLI against a fake Codex executable, never a test bridge."""

import asyncio
import json
import os
import signal
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def runner_setup(tmp_path):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!"
        + sys.executable
        + "\n"
        + textwrap.dedent("""\
        import json, os, sys, time, tomllib
        import httpx
        from pathlib import Path
        options = tomllib.loads(sys.argv[2])["mcp_servers"]["bridges"]
        events = Path(os.environ["BRIDGES_TEST_EVENTS"])
        with httpx.Client(timeout=3) as client:
            for line in sys.stdin:
                request = json.loads(line)
                method = request['method']
                if 'id' not in request:
                    continue
                if method == 'initialize':
                    message = {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
                    assert client.post(options['url'],json=message).status_code == 401
                    response = client.post(options['url'],json=message,headers={
                        "Authorization":"Bearer " + os.environ[options['bearer_token_env_var']],
                        "Accept":"application/json, text/event-stream",
                        "MCP-Protocol-Version":"2025-11-25"})
                    assert response.status_code == 200
                    assert [t['name'] for t in response.json()['result']['tools']] == ['get_protocol_docs']
                    assert 'OPENAI_API_KEY' not in os.environ and 'CODEX_API_KEY' not in os.environ
                    result = {}
                elif method == 'account/read':
                    result = {'account': None if os.environ.get('BRIDGES_TEST_NO_LOGIN') else {'type':'chatgpt'}}
                elif method in {'thread/start','thread/resume'}:
                    if method == 'thread/resume':
                        assert request['params']['threadId'] == 'owned-chat'
                    result = {'thread':{'id':'owned-chat'}}
                elif method == 'thread/read':
                    result = {'thread':{'id':'owned-chat','status':{'type':'idle'}}}
                else:
                    raise AssertionError(method)
                with events.open('a') as stream:
                    stream.write(json.dumps({'method':method,'pid':os.getpid()})+'\\n')
                print(json.dumps({'id':request['id'],'result':result}),flush=True)
                if method == 'thread/read':
                    headers = {"Authorization":"Bearer " + os.environ[options['bearer_token_env_var']],
                               "Accept":"application/json, text/event-stream",
                               "MCP-Protocol-Version":"2025-11-25"}
                    call = {"jsonrpc":"2.0","id":2,"method":"tools/call",
                            "params":{"name":"get_protocol_docs","arguments":{},"_meta":{"threadId":"owned-chat"}}}
                    for _ in range(50):
                        response = client.post(options['url'],json=call,headers=headers)
                        result = response.json()['result']
                        if not result['isError']:
                            break
                        assert result['structuredContent']['code'] == 'not_ready'
                        time.sleep(0.02)
                    assert not result['isError']
                    assert result['structuredContent']['binding']['thread_id'] == 'owned-chat'
                    call['params']['_meta']['threadId'] = 'other-chat'
                    denied = client.post(options['url'],json=call,headers=headers).json()['result']
                    assert denied['isError'] and denied['structuredContent']['code'] == 'binding_conflict'
                    with events.open('a') as stream:
                        stream.write(json.dumps({'method':'mcp_callback','pid':os.getpid()})+'\\n')
    """)
    )
    fake_codex.chmod(0o700)
    state_dir = tmp_path / "state"
    config = tmp_path / "bridges.json"
    config.write_text(
        json.dumps(
            {
                "instance_id": "test",
                "state_dir": str(state_dir),
                "codex": {"executable": str(fake_codex)},
            }
        )
    )
    events = tmp_path / "events.jsonl"
    env = {
        **os.environ,
        "BRIDGES_TEST_EVENTS": str(events),
        "OPENAI_API_KEY": "not-used",
        "CODEX_API_KEY": "not-used",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    return config, events, env, state_dir


@pytest.mark.skipif(sys.platform == "win32", reason="Bridges requires Unix process locks")
async def test_runner_owns_http_child_and_resume(runner_setup):
    config, events, env, state_dir = runner_setup
    for iteration in range(2):
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
            for _ in range(200):
                if proc.returncode is not None:
                    _, stderr = await proc.communicate()
                    pytest.fail("Runner exited before startup: " + stderr.decode())
                records = (
                    [json.loads(line) for line in events.read_text().splitlines()]
                    if events.exists()
                    else []
                )
                binding = state_dir / "test/binding.json"
                if (
                    binding.exists()
                    and sum(r["method"] == "mcp_callback" for r in records) == iteration + 1
                ):
                    break
                await asyncio.sleep(0.025)
            else:
                pytest.fail("Runner did not become ready")
            assert json.loads(binding.read_text())["thread_id"] == "owned-chat"
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
            assert proc.returncode == 0, stderr.decode()
            assert not stdout and b"Traceback" not in stderr
            for pid in {r["pid"] for r in records}:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    methods = [json.loads(line)["method"] for line in events.read_text().splitlines()]
    assert methods.count("thread/start") == 1 and methods.count("thread/resume") == 1


async def test_runner_login_failure_is_clear_and_releases_child(runner_setup):
    config, events, env, state_dir = runner_setup
    env["BRIDGES_TEST_NO_LOGIN"] = "1"
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
        assert proc.returncode == 1
        assert b"subscription_login_required:" in stderr and b"Traceback" not in stderr
        assert not stdout and not (state_dir / "test/binding.json").exists()
        records = [json.loads(line) for line in events.read_text().splitlines()]
        assert [r["method"] for r in records] == ["initialize", "account/read"]
        for pid in {r["pid"] for r in records}:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
