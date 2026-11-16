import asyncio
import base64
import json
import logging
from pathlib import Path

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, UnidentifiedImageError

from .context import ContextStore, escape_markdown
from .cron import Cron
from .envelope import Handles
from .errors import BridgeError
from .registry import CRON_NAMES, Registry
from .sessions import Sessions
from .state import identity
from .telegram import TelegramBridge, validate_text
from .transports import build_backend
from .typing import TypingManager

log = logging.getLogger(__name__)


def result(data, images=(), error=False):
    return CallToolResult(
        isError=error,
        structuredContent=data,
        content=[TextContent(type="text", text=json.dumps(data)), *images],
    )


class Runtime:
    def __init__(self, config, state, tokens, clients=None, backends=None):
        self.config, self.store = config, state
        self.registry = Registry(config)
        self.handles = Handles(config.attachment_limit, config.attachment_ttl_seconds)
        self.queue = asyncio.Queue(config.buffer_size)
        self.telegram = TelegramBridge(config, tokens, self.handles, self.enqueue, clients)
        self.backends = (
            backends
            if backends is not None
            else {name: build_backend(client) for name, client in config.clients.items()}
        )
        self.context = ContextStore(config.state_path / "context.sqlite")
        for name, backend in self.backends.items():
            backend.context_tracker = self.context.tracker(name)
        self.sessions = Sessions(config, state, self.backends)
        self.typing = {}
        self.session_queues = {name: asyncio.Queue(config.buffer_size) for name in config.sessions}
        self.consumers = []
        self.ready = False
        self.worker = None
        self.cron = (
            Cron(config.state_path, config.cron.timezone, config.telegram.connections, self.enqueue)
            if config.telegram.connections
            else None
        )

    def enqueue(self, envelope):
        try:
            self.queue.put_nowait(envelope)
            log.info("inbound_received connection_id=%s", envelope.connection_id)
            return True
        except asyncio.QueueFull:
            log.warning("inbound_dropped reason=buffer_full")
            return False

    async def start(self):
        # Startup never launches a harness or starts a model turn.
        self.ready = True
        self.consumers = [asyncio.create_task(self.consume(name)) for name in self.session_queues]
        self.worker = asyncio.create_task(self.deliver_loop())
        await self.telegram.start()
        if self.cron:
            await self.cron.start()

    def protocol_docs(self, caller):
        return self.registry.docs(caller, self.sessions.records)

    async def session(self, client_id, thread_id):
        backend = self.backends.get(client_id)
        if backend is None:
            raise BridgeError("unauthorized", "Application client is not configured.")
        return await backend.session(thread_id)

    def typing_for(self, client_id, session):
        key = (client_id, session.thread_id)
        if key not in self.typing:
            self.typing[key] = TypingManager(session, self.telegram.typing)
        return self.typing[key]

    async def stop_replaced_typing(self, name, previous):
        configured = self.config.sessions[name]
        manager = self.typing.get((configured.client, previous))
        if manager:
            for connection_id, connection in self.config.telegram.connections.items():
                if connection.session_id == name:
                    await manager.stop(connection_id)

    async def admin_command(self, envelope):
        _, connection = self.telegram.connection(envelope.connection_id)
        bot = self.config.telegram.bots[connection.bot]
        message = "Admin commands are disabled for this connection."
        if bot.admin and connection.allow_admin:
            command, argument = envelope.command
            try:
                if command == "context":
                    if argument:
                        message = "Usage: /context"
                    else:
                        record = self.sessions.record(connection.session_id)
                        configured = self.config.sessions[connection.session_id]
                        # A status request must not create a thread or start a model turn.
                        message = self.context.display(configured.client, record["thread_id"])
                elif command == "new_session":
                    if argument:
                        message = "Usage: /new_session"
                    else:
                        previous = self.sessions.record(connection.session_id)["thread_id"]
                        await self.sessions.create(connection.session_id)
                        await self.stop_replaced_typing(connection.session_id, previous)
                        message = (
                            "New empty session ready for all connections sharing this session. "
                            "Your next message starts its first turn. Previous history is saved."
                        )
                else:
                    message = await self.sessions.command(connection.session_id, command, argument)
            except BridgeError as exc:
                message = str(exc)
            except (OSError, TimeoutError, ValueError, TypeError, KeyError):
                message = "Session operation failed; check state before retrying."
        await self.telegram.clients[connection.bot].call(
            "sendMessage", {**connection.destination(), "text": message}, mutating=True
        )

    async def deliver_loop(self):
        while True:
            envelope = await self.queue.get()
            connection = self.config.telegram.connections.get(envelope.connection_id)
            try:
                if connection is None:
                    raise KeyError()
                self.session_queues[connection.session_id].put_nowait(envelope)
            except (KeyError, asyncio.QueueFull):
                log.warning("inbound_dropped reason=session_queue_unavailable")
                self.queue.task_done()

    async def consume(self, name):
        queue = self.session_queues[name]
        while True:
            envelope = await queue.get()
            try:
                async with self.sessions.locks[name]:
                    if envelope.command is not None:
                        await self.admin_command(envelope)
                        continue
                    previous = self.sessions.record(name)["thread_id"]
                    session = await self.sessions.ensure(name)
                    if previous != session.thread_id:
                        await self.stop_replaced_typing(name, previous)
                    delivery = await session.deliver(envelope.text, self.sessions.options(name))
                    log.info("inbound_delivery outcome=%s code=%s", delivery.outcome, delivery.code)
                    if delivery.outcome == "accepted" and envelope.source == "telegram":
                        client_id = self.config.sessions[name].client
                        await self.typing_for(client_id, session).start(
                            envelope.connection_id, delivery.turn_id
                        )
            except BridgeError as exc:
                log.warning("inbound_dropped code=%s", exc.code)
            except Exception:  # noqa: BLE001 - sanitize upstream failures
                log.warning("inbound_delivery outcome=unknown code=transport_failure")
            finally:
                queue.task_done()
                self.queue.task_done()

    def cron_call(self, name, arguments, client_id):
        allowed = {
            key
            for key, connection in self.config.telegram.connections.items()
            if self.config.sessions[connection.session_id].client == client_id
        }
        if name == "list_jobs" and "connection_id" not in arguments:
            return {"jobs": self.cron.list(allowed)}
        if name in {"add_job", "list_jobs"}:
            connection_id, _ = self.telegram.connection(arguments["connection_id"])
        else:
            job = self.cron.get(arguments["id"])
            connection_id = job.kwargs["connection_id"]
        if connection_id not in allowed:
            raise BridgeError(
                "unauthorized", "The job's connection is not available to this client."
            )
        if name == "add_job":
            return self.cron.add(
                connection_id, arguments["name"], arguments["pattern"], arguments["prompt"]
            )
        if name == "list_jobs":
            return {"jobs": self.cron.list({connection_id})}
        if name == "get_job_prompt":
            return {"prompt": job.kwargs["prompt"]}
        action = {
            "delete_job": self.cron.delete,
            "pause_job": self.cron.pause,
            "resume_job": self.cron.resume,
        }[name]
        return action(arguments["id"])

    async def call(self, name, arguments, meta, client_id=None):
        try:
            caller, _ = identity(meta)
            if name not in self.registry.by_name:
                raise BridgeError("unknown_tool", "This tool is not enabled.")
            if client_id not in self.backends:
                raise BridgeError("unauthorized", "Application client is not authenticated.")
            if not self.ready:
                raise BridgeError("not_ready", "Bridges is starting.")
            if name == "get_protocol_docs":
                return result(self.protocol_docs(caller))
            if name in CRON_NAMES:
                await self.session(client_id, caller)
                return result(self.cron_call(name, arguments, client_id))
            connection_id, connection = self.telegram.connection(arguments.get("connection_id"))
            await self.session(client_id, caller)
            # Outbound destinations are explicit; shared sessions never broadcast.
            if name == "telegram_send_text":
                text = arguments["text"]
                validate_text(text, 4096, "text")
                footer = escape_markdown(self.context.display(client_id, caller))
                with_footer = text + "\n\n" + footer
                # Preserve valid caller content and Telegram's existing 4096-character limit.
                omitted = len(with_footer) > 4096
                data = await self.telegram.send_text(connection, text if omitted else with_footer)
                if omitted:
                    data["context_footer_omitted"] = True
            elif name == "telegram_send_file":
                data = await self.telegram.send_file(
                    connection, arguments["path"], arguments.get("caption", "")
                )
            elif name == "telegram_send_images":
                data = await self.telegram.send_images(
                    connection, arguments["paths"], arguments.get("caption", "")
                )
            elif name == "telegram_download_file":
                data = await self.telegram.download(
                    connection_id, arguments["attachment_id"], {"file", "audio"}
                )
            else:
                files, images = [], []
                # Keep inline content bounded. Every successful download still
                # returns a path; supported small images are included inline.
                inline_bytes = 0
                for handle in arguments["attachment_ids"]:
                    file = await self.telegram.download(connection_id, handle, {"image"})
                    try:
                        with Image.open(file["path"]) as picture:
                            file.update(width=picture.width, height=picture.height)
                            mime = Image.MIME.get(picture.format)
                        if (
                            mime in {"image/png", "image/jpeg", "image/webp"}
                            and inline_bytes + file["byte_size"] <= 8 * 1024 * 1024
                        ):
                            images.append(
                                ImageContent(
                                    type="image",
                                    mimeType=mime,
                                    data=base64.b64encode(Path(file["path"]).read_bytes()).decode(),
                                )
                            )
                            inline_bytes += file["byte_size"]
                            file["inline"] = True
                    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
                        file["inline"] = False
                    files.append(file)
                return result({"files": files}, images)
            return result(data)
        except BridgeError as exc:
            return result(exc.result(), error=True)
        except (KeyError, TypeError, ValueError):
            return result(
                {
                    "code": "invalid_payload",
                    "message": "Invalid tool arguments or upstream result.",
                },
                error=True,
            )
        except Exception:  # noqa: BLE001 - sanitize third-party errors at the public boundary
            log.warning("tool_failed code=operation_failed")
            return result(
                {
                    "code": "operation_failed",
                    "message": "Operation failed; a send may have an unknown outcome. Do not blindly retry.",
                },
                error=True,
            )

    async def close(self):
        self.ready = False
        if self.cron:
            await self.cron.close()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for consumer in self.consumers:
            consumer.cancel()
        await asyncio.gather(*self.consumers, return_exceptions=True)
        for manager in self.typing.values():
            await manager.close()
        await self.telegram.close()
        for backend in self.backends.values():
            await backend.close()
        self.context.close()
