"""Telegram command parsing and application-owned session control."""

import asyncio
import json
import re
from contextlib import suppress

from .errors import BridgeError

COMMANDS = [
    {"command": "new_session", "description": "Start a new session"},
    {"command": "model", "description": "Show or change the current session model"},
    {"command": "thinking", "description": "Show or change the current thinking level"},
]
COMMAND_NAMES = {item["command"] for item in COMMANDS}
MAX_CONTROL_FRAME = 64 * 1024


def command_from(message, username=None):
    """Only an actual leading Telegram bot-command entity is a control request."""
    text = message.get("text", "")
    entities = message.get("entities", [])
    if not isinstance(text, str) or not any(
        entity.get("type") == "bot_command" and entity.get("offset") == 0
        for entity in entities
        if isinstance(entity, dict)
    ):
        return None
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    match = re.fullmatch(r"/([a-z_]+)(?:@([A-Za-z0-9_]+))?", parts[0])
    if not match or match[1] not in COMMAND_NAMES:
        return None
    if match[2] and username and match[2].casefold() != username.casefold():
        return None
    return match[1], parts[1].strip() if len(parts) == 2 else ""


class ApplicationControl:
    """One bounded JSON-line exchange over a protected application Unix socket.

    The application, rather than Bridges, creates threads and changes settings.
    Mutations with uncertain outcomes are never retried.
    """

    def __init__(self, path):
        self.path = path

    async def turn_settings(self, thread_id):
        response = await self.request(None, thread_id, "turn_settings", "")
        options = response.get("turn_options")
        if (
            not isinstance(options, dict)
            or set(options) - {"model", "effort"}
            or any(not isinstance(value, str) or not value for value in options.values())
        ):
            raise BridgeError("admin_unavailable", "Invalid application turn settings.")
        return options

    async def request(self, connection_id, thread_id, command, argument):
        if not self.path:
            raise BridgeError("admin_unavailable", "Application control is not configured.")
        writer = None
        try:
            async with asyncio.timeout(15):
                reader, writer = await asyncio.open_unix_connection(
                    self.path, limit=MAX_CONTROL_FRAME
                )
                payload = {
                    "connection_id": connection_id,
                    "thread_id": thread_id,
                    "command": command,
                    "argument": argument,
                }
                writer.write(json.dumps(payload).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
                if (
                    isinstance(response, dict)
                    and isinstance(response.get("error"), str)
                    and 1 <= len(response["error"]) <= 4096
                ):
                    raise BridgeError("admin_rejected", response["error"])
                if (
                    not isinstance(response, dict)
                    or not isinstance(response.get("thread_id"), str)
                    or not response["thread_id"]
                    or not isinstance(response.get("message"), str)
                    or not 1 <= len(response["message"]) <= 4096
                    or (command != "new_session" and response["thread_id"] != thread_id)
                    or (command == "new_session" and response["thread_id"] == thread_id)
                ):
                    raise ValueError("Invalid control response")
                return response
        except (OSError, TimeoutError, ValueError, TypeError):
            raise BridgeError(
                "admin_outcome_unknown",
                "Application control failed; check session state before retrying.",
            ) from None
        finally:
            if writer:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
