import argparse
import asyncio
import contextlib
import json
import logging
import secrets
import signal
import socket
import sys
from pathlib import Path

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from .config import load_config
from .errors import BridgeError
from .runtime import Runtime
from .server import create_server
from .state import InstanceState
from .telegram import TelegramAPI
from .transports.app_server import AppServerTransport


class BearerEndpoint:
    def __init__(self, manager, bearer):
        self.manager, self.bearer = manager, bearer

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        expected = ("Bearer " + self.bearer).encode()
        if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        await self.manager.handle_request(scope, receive, send)


async def run_app_server(runtime, server):
    bearer = secrets.token_urlsafe(32)
    manager = StreamableHTTPSessionManager(
        server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*"], allowed_origins=[]
        ),
    )
    endpoint = BearerEndpoint(manager, bearer)
    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST", "GET", "DELETE"])])
    sock = socket.socket()
    sock.bind(("127.0.0.1", runtime.config.codex.port))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    http = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
    )
    http.capture_signals = contextlib.nullcontext
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    http_task = None
    waiters = []
    try:
        async with manager.run():
            http_task = asyncio.create_task(http.serve(sockets=[sock]))
            while not http.started:
                if http_task.done():
                    await http_task
                    raise BridgeError(
                        "endpoint_unavailable", "Loopback MCP endpoint failed to start."
                    )
                await asyncio.sleep(0.01)
            await runtime.start(f"http://127.0.0.1:{port}/mcp", bearer)
            logging.getLogger(__name__).info("app_server_ready")
            waiters = [
                asyncio.create_task(stop.wait()),
                asyncio.create_task(runtime.transport.proc.wait()),
            ]
            await asyncio.wait([*waiters, http_task], return_when=asyncio.FIRST_COMPLETED)
            if not stop.is_set():
                raise BridgeError(
                    "codex_disconnected",
                    "App Server or MCP endpoint exited; restart Bridges to resume the binding.",
                )
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        http.should_exit = True
        if http_task:
            try:
                await asyncio.wait_for(http_task, 5)
            except TimeoutError:
                http_task.cancel()
                await asyncio.gather(http_task, return_exceptions=True)
        sock.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


async def run(config, tokens):
    state = InstanceState(config.state_path, config.instance_id)
    state.acquire(tokens.values())
    transport = AppServerTransport(config.codex)
    runtime = None
    try:
        runtime = Runtime(config, state, transport, tokens)
        server = create_server(runtime)
        await run_app_server(runtime, server)
    except BaseExceptionGroup as group:
        # The SDK's task group wraps startup errors. Keep expected operational
        # failures concise at the CLI boundary, including missing subscription login.
        cause = group
        while isinstance(cause, BaseExceptionGroup) and len(cause.exceptions) == 1:
            cause = cause.exceptions[0]
        if isinstance(cause, (BridgeError, OSError, TimeoutError)):
            raise cause from None
        raise
    finally:
        if runtime:
            await runtime.close()
        else:
            await transport.close()
        state.close()


async def discover_telegram(config, bot=None):
    if bot is None:
        if len(config.telegram.bots) != 1:
            raise BridgeError(
                "ambiguous_bot", "Specify --bot when more than one bot is configured."
            )
        bot = next(iter(config.telegram.bots))
    token = config.token(bot)
    state = InstanceState(config.state_path, config.instance_id)
    state.acquire([token])
    api = TelegramAPI(token)
    try:
        me = await api.call("getMe")
        if (await api.call("getWebhookInfo")).get("url"):
            raise BridgeError(
                "telegram_conflict",
                "This bot has a webhook. Remove it explicitly before using polling.",
            )
        updates = await api.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        chats = {}
        for update in updates:
            message = update.get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") not in {"private", "group", "supergroup"}:
                continue
            topic = message.get("message_thread_id")
            chats[(str(chat["id"]), topic)] = {
                "chat_id": str(chat["id"]),
                "type": chat["type"],
                **(
                    {"label": chat.get("title") or chat.get("first_name")}
                    if chat.get("title") or chat.get("first_name")
                    else {}
                ),
                **({"message_thread_id": topic} if topic else {}),
            }
        print(
            json.dumps(
                {
                    "bot_username": me.get("username"),
                    "chats": list(chats.values()),
                    "hint": "Send this bot /start (or a new message), then run discovery again if no chats appear. Stop other pollers first.",
                },
                indent=2,
            )
        )
    finally:
        await api.close()
        state.close()


def main():
    parser = argparse.ArgumentParser(
        description="Telegram bridge with one persistent Codex App Server session."
    )
    parser.add_argument(
        "command",
        choices=["serve", "validate", "reset-binding", "discover-telegram"],
        nargs="?",
        default="serve",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bot", help="Configured bot ID for discover-telegram")
    args = parser.parse_args()
    # HTTP libraries may log token-bearing URLs at INFO. Keep them silent.
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    logging.getLogger("bridges").setLevel(logging.INFO)
    for logger in ("httpx", "httpcore", "mcp"):
        logging.getLogger(logger).setLevel(logging.CRITICAL)
    try:
        config = load_config(args.config)
        if args.command == "discover-telegram":
            asyncio.run(discover_telegram(config, args.bot))
        elif args.command == "reset-binding":
            state = InstanceState(config.state_path, config.instance_id)
            state.acquire()
            try:
                state.reset()
            finally:
                state.close()
            print("Binding reset. The next start creates a new App Server chat.")
        elif args.command == "validate":
            config.tokens()
            print("Configuration and token environment variables are valid.")
        else:
            asyncio.run(run(config, config.tokens()))
    except BridgeError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    except (OSError, TimeoutError):
        print(
            "startup_failed: Check executable, state directory, and local transport availability.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
