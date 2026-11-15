import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

import yaml

from .errors import BridgeError


@dataclass(frozen=True)
class Inbound:
    connection_id: str
    text: str
    command: tuple[str, str] | None = None
    source: str = "telegram"


@dataclass
class Attachment:
    connection_id: str
    bot: str
    kind: str
    file_id: str
    name: str | None = None
    mime: str | None = None
    size: int | None = None


class Handles:
    def __init__(self, limit=4096, ttl=86400, clock=time.monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self.entries = OrderedDict()

    def add(self, attachment):
        handle = "a_" + secrets.token_hex(16)
        self.entries[handle] = (self.clock() + self.ttl, attachment)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return handle

    def get(self, handle, connection_id, kinds):
        entry = self.entries.get(handle)
        if entry is None or entry[0] <= self.clock():
            self.entries.pop(handle, None)
            raise BridgeError(
                "attachment_expired",
                "Attachment is unavailable after expiry, eviction, or restart.",
            )
        item = entry[1]
        if item.connection_id != connection_id:
            raise BridgeError(
                "attachment_expired", "Attachment is not available for this connection."
            )
        if item.kind not in kinds:
            raise BridgeError(
                "invalid_attachment_type",
                "This download tool does not accept that attachment type.",
            )
        return item


def normalize(update, bot, connections, handles, now=None):
    message = update.get("message")
    if not isinstance(message, dict) or message.get("chat", {}).get("type") not in {
        "private",
        "group",
        "supergroup",
    }:
        return None
    routes = [
        (key, c)
        for key, c in connections.items()
        if c.bot == bot
        and c.chat_id == str(message["chat"]["id"])
        and (c.message_thread_id is None or c.message_thread_id == message.get("message_thread_id"))
    ]
    if not routes:
        return None
    connection_id, _ = routes[0]
    kind, file = None, None
    if message.get("photo"):
        kind, file = (
            "image",
            max(message["photo"], key=lambda p: p.get("width", 0) * p.get("height", 0)),
        )
    elif message.get("voice") or message.get("audio"):
        kind, file = "audio", message.get("voice") or message["audio"]
    elif message.get("document"):
        kind, file = "file", message["document"]
    elif not isinstance(message.get("text"), str):
        return None
    header = {
        "bridge": "telegram",
        "connection_id": connection_id,
        "message_id": message["message_id"],
        "received_at": (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    reply = message.get("reply_to_message")
    if isinstance(reply, dict) and "message_id" in reply:
        header["reply_to_message_id"] = reply["message_id"]
    if file:
        handle = handles.add(
            Attachment(
                connection_id,
                bot,
                kind,
                file["file_id"],
                file.get("file_name"),
                file.get("mime_type"),
                file.get("file_size"),
            )
        )
        header["attachments"] = [{"type": kind, "id": handle}]
    body = message.get("text", message.get("caption", ""))
    text = (
        "```yaml\n"
        + yaml.safe_dump({"bridge_context": header}, sort_keys=False, allow_unicode=True)
        + "```\n\n"
        + body
    )
    return Inbound(connection_id, text)
