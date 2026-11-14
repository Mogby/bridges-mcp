from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, UnidentifiedImageError

from .envelope import normalize
from .errors import BridgeError

log = logging.getLogger(__name__)
DOWNLOAD_LIMIT = 20 * 1024 * 1024


class TelegramAPI:
    def __init__(self, token, client=None):
        self._base = "https://api.telegram.org/bot" + token + "/"
        self._files = "https://api.telegram.org/file/bot" + token + "/"
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(40, connect=10), follow_redirects=False
        )

    async def call(self, method, data=None, files=None, mutating=False):
        try:
            if files:
                response = await self.client.post(self._base + method, data=data, files=files)
            else:
                response = await self.client.post(self._base + method, json=data or {})
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise BridgeError(
                "delivery_unknown" if mutating else "telegram_unavailable",
                "Telegram did not return a valid acknowledgement; do not blindly retry a send.",
            ) from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            code = payload.get("error_code") if isinstance(payload, dict) else None
            if code == 429:
                delay = payload.get("parameters", {}).get("retry_after", 4)
                raise BridgeError(
                    "rate_limited",
                    "Telegram rate limited this operation; it was not accepted.",
                    retry_after=delay,
                )
            if code == 409:
                raise BridgeError(
                    "telegram_conflict", "Telegram reports a webhook or another poller conflict."
                )
            # Do not echo Telegram descriptions: they may contain sensitive inputs.
            raise BridgeError("telegram_unavailable", "Telegram rejected the operation.")
        return payload["result"]

    async def download(self, attachment, directory):
        if attachment.size and attachment.size > DOWNLOAD_LIMIT:
            raise BridgeError("file_too_large", "Telegram downloads are limited to 20 MB.")
        info = await self.call("getFile", {"file_id": attachment.file_id})
        if info.get("file_size", 0) > DOWNLOAD_LIMIT:
            raise BridgeError("file_too_large", "Telegram downloads are limited to 20 MB.")
        remote = info.get("file_path")
        if not isinstance(remote, str) or remote.startswith("/") or ".." in remote.split("/"):
            raise BridgeError("telegram_unavailable", "Telegram returned an invalid file path.")
        suffix = Path(remote).suffix.lower()
        if len(suffix) > 12 or not suffix.removeprefix(".").isalnum():
            suffix = ".bin"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (secrets.token_hex(16) + suffix)
        size = 0
        try:
            async with self.client.stream("GET", self._files + quote(remote, safe="/")) as response:
                response.raise_for_status()
                with path.open("xb") as output:
                    path.chmod(0o600)
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > DOWNLOAD_LIMIT:
                            raise BridgeError(
                                "file_too_large", "Telegram downloads are limited to 20 MB."
                            )
                        output.write(chunk)
        except BaseException as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, httpx.HTTPError):
                raise BridgeError(
                    "telegram_unavailable", "Telegram file download failed."
                ) from None
            raise
        return {
            "path": str(path),
            "name": attachment.name,
            "mime_type": attachment.mime
            or mimetypes.guess_type(path)[0]
            or "application/octet-stream",
            "byte_size": size,
        }

    async def close(self):
        await self.client.aclose()


def validate_text(text, limit, field, allow_empty=False):
    # Conservatively bound MarkdownV2 source length; Telegram validates the
    # parsed text and formatting itself. Do not escape away intended markup.
    if not isinstance(text, str) or len(text) > limit or (not allow_empty and not text):
        raise BridgeError(
            "invalid_payload",
            f"{field} must contain {'0' if allow_empty else '1'}–{limit} characters.",
        )


def upload_path(value, image=False):
    path = Path(value).expanduser().resolve()
    try:
        if not path.is_file():
            raise OSError()
        size = path.stat().st_size
        if size > (10 if image else 50) * 1024 * 1024:
            raise BridgeError(
                "file_too_large", "Upload exceeds Telegram's photo (10 MB) or file (50 MB) limit."
            )
        with path.open("rb"):
            pass
        if image:
            with Image.open(path) as picture:
                width, height = picture.size
                if width + height > 10000 or max(width, height) / min(width, height) > 20:
                    raise BridgeError("invalid_payload", "Photo dimensions exceed Telegram limits.")
                if picture.format not in {"JPEG", "PNG", "WEBP"}:
                    raise BridgeError(
                        "invalid_payload", "Use JPEG, PNG, or WebP photos, or send this as a file."
                    )
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
        raise BridgeError(
            "invalid_payload", "Upload must be a readable local file of the requested type."
        ) from None
    return path


class TelegramBridge:
    def __init__(self, config, tokens, handles, enqueue, clients=None, sleep=asyncio.sleep):
        self.config, self.handles, self.enqueue = config, handles, enqueue
        self.clients = (
            clients
            if clients is not None
            else {key: TelegramAPI(token) for key, token in tokens.items()}
        )
        self.sleep = sleep
        self.tasks = []

    def connection(self, name=None):
        connections = self.config.telegram.connections
        if name is None:
            if len(connections) != 1:
                raise BridgeError(
                    "ambiguous_connection",
                    "Specify connection_id when more than one connection is configured.",
                )
            name = next(iter(connections))
        if name not in connections:
            raise BridgeError("unknown_connection", "The connection_id is not configured.")
        return name, connections[name]

    async def start(self):
        if not self.tasks:
            self.tasks = [asyncio.create_task(self.poll(bot)) for bot in self.clients]

    async def poll(self, bot):
        api, offset, seen = self.clients[bot], None, OrderedDict()
        checked = False
        backoff = 1
        while True:
            try:
                if not checked:
                    webhook = await api.call("getWebhookInfo")
                    if webhook.get("url"):
                        raise BridgeError(
                            "telegram_conflict",
                            "Remove the configured webhook explicitly before starting polling.",
                        )
                    checked = True
                updates = await api.call(
                    "getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]}
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int):
                        continue
                    offset = max(offset or 0, update_id + 1)
                    if update_id in seen:
                        continue
                    seen[update_id] = None
                    while len(seen) > 2048:
                        seen.popitem(last=False)
                    try:
                        envelope = normalize(
                            update, bot, self.config.telegram.connections, self.handles
                        )
                        if envelope:
                            self.enqueue(envelope)
                    except (KeyError, TypeError, ValueError):
                        log.warning("telegram_update_dropped reason=invalid_payload")
                backoff = 1
            except BridgeError as exc:
                log.warning("telegram_poll_failed code=%s", exc.code)
                await self.sleep(min(60, max(backoff, exc.details.get("retry_after", 0))))
                backoff = min(60, backoff * 2)
                checked = False

    async def send_text(self, connection, text):
        validate_text(text, 4096, "text")
        result = await self.clients[connection.bot].call(
            "sendMessage",
            {**connection.destination(), "text": text, "parse_mode": "MarkdownV2"},
            mutating=True,
        )
        return {"message_id": result["message_id"]}

    async def send_file(self, connection, path, caption=""):
        validate_text(caption, 1024, "caption", True)
        path = upload_path(path)
        with path.open("rb") as stream:
            result = await self.clients[connection.bot].call(
                "sendDocument",
                {**connection.destination(), "caption": caption, "parse_mode": "MarkdownV2"},
                files={"document": (path.name, stream)},
                mutating=True,
            )
        return {"message_id": result["message_id"]}

    async def send_images(self, connection, paths, caption=""):
        validate_text(caption, 1024, "caption", True)
        if not paths or len(paths) > 100:
            raise BridgeError("invalid_payload", "Supply between 1 and 100 image paths.")
        validated = [upload_path(p, image=True) for p in paths]
        completed = []
        for index, path in enumerate(validated):
            try:
                with path.open("rb") as stream:
                    result = await self.clients[connection.bot].call(
                        "sendPhoto",
                        {
                            **connection.destination(),
                            "caption": caption if index == 0 else "",
                            "parse_mode": "MarkdownV2",
                        },
                        files={"photo": (path.name, stream)},
                        mutating=True,
                    )
                completed.append(result["message_id"])
            except (BridgeError, OSError) as exc:
                raise BridgeError(
                    exc.code if isinstance(exc, BridgeError) else "invalid_payload",
                    "Image batch stopped; inspect completed IDs and failed index before retrying.",
                    completed_message_ids=completed,
                    failed_index=index,
                    failed_delivery_unknown=isinstance(exc, BridgeError)
                    and exc.code == "delivery_unknown",
                ) from None
        return {"message_ids": completed}

    async def download(self, connection_id, attachment_id, kinds):
        item = self.handles.get(attachment_id, connection_id, kinds)
        return await self.clients[item.bot].download(item, self.config.downloads)

    async def typing(self, connection_id):
        _, connection = self.connection(connection_id)
        await self.clients[connection.bot].call(
            "sendChatAction", {**connection.destination(), "action": "typing"}
        )

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(api.close() for api in self.clients.values()))
