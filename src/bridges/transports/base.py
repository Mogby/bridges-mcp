import asyncio
from dataclasses import dataclass
from typing import Literal


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
