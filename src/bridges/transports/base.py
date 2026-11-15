import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class TurnState:
    status: Literal["active", "idle", "unknown", "disconnected"] = "unknown"
    turn_id: str | None = None


@dataclass(frozen=True)
class Delivery:
    outcome: Literal["accepted", "rejected", "unknown"]
    code: str
    turn_id: str | None = None


class Transport:
    def __init__(self):
        self.thread_id = None
        self.state = TurnState()
        self.listeners = []
        self.delivery_lock = asyncio.Lock()

    def update(self, state):
        self.state = state
        for listener in self.listeners:
            listener(state)

    async def verify_turn(self, claimed=None):
        state = await self.snapshot()
        if state.status != "active" or not state.turn_id or (claimed and state.turn_id != claimed):
            return None
        return state.turn_id


@dataclass(frozen=True)
class ModelInfo:
    id: str
    aliases: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None


class Backend(Protocol):
    """Harness adapter contract; response shapes are normalized by adapters."""

    async def connect(self): ...
    async def create(self, cwd: str, options: dict) -> dict: ...
    async def load(self, thread_id: str, cwd: str, options: dict) -> Transport: ...
    async def session(self, thread_id: str) -> Transport: ...
    async def models(self) -> list[ModelInfo]: ...
    async def close(self): ...
