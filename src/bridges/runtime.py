import asyncio
import base64
import json
import logging
from pathlib import Path

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, UnidentifiedImageError

from .envelope import Handles
from .errors import BridgeError
from .registry import Registry
from .state import identity
from .telegram import TelegramBridge
from .transports.app_server import AppServer
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
            else {name: AppServer(client) for name, client in config.clients.items()}
        )
        self.focuses = state.read_focus()
        self.typing = {}
        self.connection_locks = {key: asyncio.Lock() for key in config.telegram.connections}
        self.ready = False
        self.worker = None

    def enqueue(self, envelope):
        try:
            self.queue.put_nowait(envelope)
        except asyncio.QueueFull:
            log.warning("inbound_dropped reason=buffer_full")

    async def start(self):
        # No Codex process or conversation is started here. A client may be offline.
        self.ready = True
        self.worker = asyncio.create_task(self.deliver_loop())
        await self.telegram.start()

    def protocol_docs(self, caller):
        return self.registry.docs(caller, self.focuses)

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

    async def focus(self, connection_id, client_id, caller):
        await self.session(client_id, caller)
        target = {"client_id": client_id, "thread_id": caller}
        previous = self.focuses.get(connection_id)
        if previous != target:
            if previous:
                manager = self.typing.get((previous["client_id"], previous["thread_id"]))
                if manager:
                    await manager.stop(connection_id)
            updated = {**self.focuses, connection_id: target}
            self.store.save_focus(updated)
            self.focuses = updated
        return {"connection_id": connection_id, **target}

    async def deliver_loop(self):
        while True:
            envelope = await self.queue.get()
            try:
                async with self.connection_locks[envelope.connection_id]:
                    target = self.focuses.get(envelope.connection_id)
                    if not target:
                        log.info("inbound_dropped reason=unfocused")
                        continue
                    session = await self.session(target["client_id"], target["thread_id"])
                    delivery = await session.deliver(envelope.text)
                    log.info("inbound_delivery outcome=%s code=%s", delivery.outcome, delivery.code)
                    if delivery.outcome == "accepted":
                        await self.typing_for(target["client_id"], session).start(
                            envelope.connection_id, delivery.turn_id
                        )
            except BridgeError as exc:
                log.warning("inbound_dropped code=%s", exc.code)
            except Exception:  # noqa: BLE001 - sanitize upstream failures
                log.warning("inbound_delivery outcome=unknown code=transport_failure")
            finally:
                self.queue.task_done()

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
            connection_id, connection = self.telegram.connection(arguments.get("connection_id"))
            async with self.connection_locks[connection_id]:
                if name == "focus":
                    return result(await self.focus(connection_id, client_id, caller))
                await self.session(client_id, caller)
            # Outbound tools have explicit destinations; focus controls inbound routing
            # and typing, not the user's choice of where to send a message.
            if name == "telegram_send_text":
                data = await self.telegram.send_text(connection, arguments["text"])
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
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for manager in self.typing.values():
            await manager.close()
        await self.telegram.close()
        for backend in self.backends.values():
            await backend.close()
