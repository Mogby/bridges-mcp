"""Trusted Telegram admin command parsing."""

import re

COMMANDS = [
    {"command": "context", "description": "Show context usage and observed compactions"},
    {"command": "new_session", "description": "Start a new session"},
    {"command": "model", "description": "Show or change the current session model"},
    {"command": "thinking", "description": "Show or change the current thinking level"},
]
COMMAND_NAMES = {item["command"] for item in COMMANDS}


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
