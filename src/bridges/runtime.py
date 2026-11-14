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
from .typing import TypingManager

log = logging.getLogger(__name__)


def result(data, images=(), error=False):
    return CallToolResult(
        isError=error,
        structuredContent=data,
        content=[TextContent(type="text", text=json.dumps(data)), *images],
    )


class Runtime:
    def __init__(self, config, state, transport, tokens, clients=None):
        self.config, self.store, self.transport = config, state, transport
        self.registry = Registry(config)
        self.handles = Handles(config.attachment_limit, config.attachment_ttl_seconds)
        self.queue = asyncio.Queue(config.buffer_size)
        self.telegram = TelegramBridge(config, tokens, self.handles, self.enqueue, clients)
        self.typing = TypingManager(transport, self.telegram.typing)
        self.thread_id = state.read()
        self.ready = False
        self.worker = None
        self.bind_lock = asyncio.Lock()

    def enqueue(self, envelope):
        try:
            self.queue.put_nowait(envelope)
        except asyncio.QueueFull:
            log.warning("inbound_dropped reason=buffer_full")

    async def start_bound(self):
        self.ready = True
        if self.worker is None:
            self.worker = asyncio.create_task(self.deliver_loop())
        await self.telegram.start()

    async def bootstrap(self, caller):
        async with self.bind_lock:
            if self.thread_id and self.thread_id != caller:
                raise BridgeError(
                    "binding_conflict",
                    "This Bridges instance is bound to a different chat. Use a local reset to change it.",
                )
            if not self.ready:
                await self.transport.bind(caller)
                self.store.save(caller)
                self.thread_id = caller
                await self.start_bound()
            return self.registry.docs(self.thread_id)

    async def restore(self):
        if self.thread_id:
            await self.transport.bind(self.thread_id)
            await self.start_bound()

    async def deliver_loop(self):
        while True:
            envelope = await self.queue.get()
            try:
                delivery = await self.transport.deliver(envelope)
                log.info("inbound_delivery outcome=%s code=%s", delivery.outcome, delivery.code)
            except Exception:  # noqa: BLE001 - sanitize third-party errors at the public boundary
                # No body, exception repr, upstream URL, or retry on uncertainty.
                log.warning("inbound_delivery outcome=unknown code=transport_failure")
            finally:
                self.queue.task_done()

    async def call(self, name, arguments, meta):
        try:
            caller, turn = identity(meta)
            if name not in self.registry.by_name:
                raise BridgeError("unknown_tool", "This tool is not enabled.")
            if name == "get_protocol_docs":
                return result(await self.bootstrap(caller))
            if not self.ready or caller != self.thread_id:
                raise BridgeError(
                    "binding_conflict",
                    "Only the bound chat may use communication tools; initialize with get_protocol_docs.",
                )
            connection_id, connection = self.telegram.connection(arguments.get("connection_id"))
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
            elif name == "telegram_set_typing":
                data = await self.typing.set(connection_id, arguments["enabled"], turn)
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
        await self.typing.close()
        await self.telegram.close()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.transport.close()
