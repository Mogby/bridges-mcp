from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import BridgeError


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Bot(Model):
    token_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    admin: bool = False


class Connection(Model):
    bot: str
    allow_admin: bool = False
    chat_id: StrictStr = Field(pattern=r"^-?[0-9]+$")
    message_thread_id: int | None = Field(default=None, gt=0)
    label: str | None = None

    def destination(self):
        result = {"chat_id": self.chat_id}
        if self.message_thread_id is not None:
            result["message_thread_id"] = self.message_thread_id
        return result


class Telegram(Model):
    bots: dict[str, Bot] = Field(default_factory=dict)
    connections: dict[str, Connection] = Field(default_factory=dict)

    @field_validator("connections", mode="before")
    @classmethod
    def empty_connections(cls, value):
        return {} if value == [] else value

    @model_validator(mode="after")
    def routes(self):
        routes = []
        for name, connection in self.connections.items():
            if not name.strip() or connection.bot not in self.bots:
                raise ValueError("Connection must have a nonempty ID and a configured bot")
            for other in routes:
                if (other.bot, other.chat_id) == (connection.bot, connection.chat_id) and (
                    other.message_thread_id is None
                    or connection.message_thread_id is None
                    or other.message_thread_id == connection.message_thread_id
                ):
                    raise ValueError("Telegram routes overlap")
            routes.append(connection)
        return self


class Client(Model):
    token_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    socket_path: str = Field(pattern=r"^/")
    control_socket_path: str | None = Field(default=None, pattern=r"^/")


class HTTP(Model):
    port: int = Field(default=8765, ge=1, le=65535)


class Config(Model):
    instance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    http: HTTP = Field(default_factory=HTTP)
    clients: dict[str, Client] = Field(default_factory=dict)
    telegram: Telegram = Field(default_factory=Telegram)
    state_dir: str = "~/.local/state/bridges"
    download_dir: str | None = None
    buffer_size: int = Field(default=256, gt=0, le=10000)
    attachment_limit: int = Field(default=4096, gt=0, le=100000)
    attachment_ttl_seconds: int = Field(default=86400, gt=0)

    @property
    def state_path(self):
        return Path(self.state_dir).expanduser().resolve()

    @property
    def downloads(self):
        return (
            Path(self.download_dir).expanduser().resolve()
            if self.download_dir
            else self.state_path / self.instance_id / "downloads"
        )

    def client_tokens(self):
        result = {}
        for name, client in self.clients.items():
            value = os.environ.get(client.token_env, "")
            if not name.strip() or len(value) < 32 or value in result.values():
                raise BridgeError(
                    "invalid_config",
                    "Each client needs a unique bearer token of at least 32 characters.",
                )
            result[name] = value
        if not result:
            raise BridgeError(
                "invalid_config", "Configure at least one authenticated application client."
            )
        return result

    def tokens(self):
        result = {}
        for bot in {c.bot for c in self.telegram.connections.values()}:
            value = self.token(bot)
            if value.split(":", 1)[0] in {token.split(":", 1)[0] for token in result.values()}:
                raise BridgeError(
                    "invalid_config",
                    "Use one bot entry for each token; duplicate bot accounts are not allowed.",
                )
            result[bot] = value
        return result

    def token(self, bot):
        if bot not in self.telegram.bots:
            raise BridgeError("invalid_config", "The selected bot ID is not configured.")
        value = os.environ.get(self.telegram.bots[bot].token_env, "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", value):
            raise BridgeError(
                "invalid_config",
                "A configured bot token environment variable is missing or invalid.",
            )
        return value


def load_config(path: Path) -> Config:
    try:
        return Config.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        # ValidationError can contain input values: do not print it.
        raise BridgeError(
            "invalid_config", "Invalid configuration; check field names, types, routes, and paths."
        ) from exc
