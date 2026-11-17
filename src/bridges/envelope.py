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


def positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def sender_metadata(message):
    """Use Telegram's actual sender, not a forwarded origin or anonymous proxy."""
    if "sender_chat" in message:
        sender = message["sender_chat"]
        kind = "chat"
    else:
        sender = message.get("from")
        kind = "user"
    if (
        not isinstance(sender, dict)
        or not isinstance(sender.get("id"), int)
        or isinstance(sender["id"], bool)
        or sender["id"] == 0
        or (kind == "user" and sender["id"] < 0)
    ):
        return None
    result = {"type": kind, "id": sender["id"]}
    if kind == "user" and isinstance(sender.get("is_bot"), bool):
        result["is_bot"] = sender["is_bot"]
    if isinstance(sender.get("username"), str):
        result["username"] = sender["username"]
    return result


class MessageRoutes:
    """Bounded routing metadata only; no message content or history lookup."""

    def __init__(self, limit=8192, ttl=86400, clock=time.monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self.entries = OrderedDict()

    def remember(self, bot, connection, message_id):
        if not positive_id(message_id):
            return
        key = (bot, connection.chat_id, message_id)
        self.entries[key] = (self.clock() + self.ttl, connection.message_thread_id)
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)

    def matches(self, bot, connection, message_id):
        key = (bot, connection.chat_id, message_id)
        entry = self.entries.get(key)
        if entry is None or entry[0] <= self.clock():
            self.entries.pop(key, None)
            return False
        return entry[1] == connection.message_thread_id


def reaction_types(value):
    """Copy supported Telegram reaction types, including received paid reactions."""
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind == "emoji" and isinstance(item.get("emoji"), str) and item["emoji"]:
            result.append({"type": kind, "emoji": item["emoji"]})
        elif (
            kind == "custom_emoji"
            and isinstance(item.get("custom_emoji_id"), str)
            and item["custom_emoji_id"].isascii()
            and item["custom_emoji_id"].isdigit()
        ):
            result.append({"type": kind, "custom_emoji_id": item["custom_emoji_id"]})
        elif kind == "paid":
            result.append({"type": kind})
        else:
            return None
    return result


def received_at(now):
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode(header, body=""):
    return (
        "```yaml\n"
        + yaml.safe_dump({"bridge_context": header}, sort_keys=False, allow_unicode=True)
        + "```\n\n"
        + body
    )


def normalize_reaction(update, bot, connections, routes, now):
    event = next(
        (key for key in ("message_reaction", "message_reaction_count") if key in update), None
    )
    if event is None:
        return None
    payload = update[event]
    if not isinstance(payload, dict) or not isinstance(payload.get("chat"), dict):
        return None
    chat, message_id = payload["chat"], payload.get("message_id")
    update_id, date = update.get("update_id"), payload.get("date")
    if (
        not isinstance(chat.get("id"), int)
        or isinstance(chat["id"], bool)
        or chat["id"] == 0
        or not positive_id(message_id)
        or not isinstance(update_id, int)
        or isinstance(update_id, bool)
        or update_id < 0
        or not isinstance(date, int)
        or isinstance(date, bool)
        or date < 0
    ):
        return None
    matches = [
        (key, connection)
        for key, connection in connections.items()
        if connection.bot == bot
        and connection.chat_id == str(chat.get("id"))
        and (
            connection.message_thread_id is None
            or (routes is not None and routes.matches(bot, connection, message_id))
        )
    ]
    if len(matches) != 1:
        return None
    connection_id, _ = matches[0]
    reaction = {"date": date}
    if event == "message_reaction":
        old, new = (
            reaction_types(payload.get("old_reaction")),
            reaction_types(payload.get("new_reaction")),
        )
        if old is None or new is None:
            return None
        actor, actor_type = payload.get("user"), "user"
        if actor is None:
            actor, actor_type = payload.get("actor_chat"), "chat"
        if (
            not isinstance(actor, dict)
            or not isinstance(actor.get("id"), int)
            or isinstance(actor["id"], bool)
        ):
            return None
        reaction.update(
            actor={"type": actor_type, "id": actor["id"]}, old_reaction=old, new_reaction=new
        )
    else:
        counts = payload.get("reactions")
        if not isinstance(counts, list):
            return None
        reaction["reactions"] = []
        for count in counts:
            if not isinstance(count, dict):
                return None
            kinds = reaction_types([count.get("type")])
            total = count.get("total_count")
            if kinds is None or not isinstance(total, int) or isinstance(total, bool) or total < 0:
                return None
            reaction["reactions"].append({"type": kinds[0], "total_count": total})
    header = {
        "bridge": "telegram",
        "connection_id": connection_id,
        "message_id": message_id,
        "received_at": received_at(now),
        "event": event,
        "update_id": update_id,
        "reaction": reaction,
    }
    return Inbound(connection_id, encode(header))


def normalize(update, bot, connections, handles, now=None, routes=None):
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return normalize_reaction(update, bot, connections, routes, now)
    if not isinstance(message.get("chat"), dict) or message["chat"].get("type") not in {
        "private",
        "group",
        "supergroup",
    }:
        return None
    matches = [
        (key, c)
        for key, c in connections.items()
        if c.bot == bot
        and c.chat_id == str(message["chat"]["id"])
        and (c.message_thread_id is None or c.message_thread_id == message.get("message_thread_id"))
    ]
    if not matches:
        return None
    connection_id, connection = matches[0]
    if not positive_id(message.get("message_id")):
        return None
    if routes is not None:
        routes.remember(bot, connection, message["message_id"])
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
        "received_at": received_at(now),
    }
    sender = sender_metadata(message)
    if sender is not None:
        header["sender"] = sender
    reply = message.get("reply_to_message")
    if isinstance(reply, dict) and "message_id" in reply:
        header["reply_to_message_id"] = reply["message_id"]
    quote = message.get("quote")
    if (
        isinstance(quote, dict)
        and isinstance(quote.get("text"), str)
        and isinstance(quote.get("position"), int)
        and not isinstance(quote["position"], bool)
        and quote["position"] >= 0
    ):
        header["quote"] = {"text": quote["text"], "position": quote["position"]}
        for key in ("entities", "rich_texts"):
            if isinstance(quote.get(key), list):
                header["quote"][key] = quote[key]
        if isinstance(quote.get("is_manual"), bool):
            header["quote"]["is_manual"] = quote["is_manual"]
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
    return Inbound(connection_id, encode(header, body))
