"""Best-effort typing owned by accepted inbound turns, never by agent tools."""

import asyncio
import logging
from dataclasses import dataclass

from .errors import BridgeError
from .transports.base import TurnState

log = logging.getLogger(__name__)


@dataclass
class Owner:
    turn_id: str
    task: asyncio.Task | None = None


class TypingManager:
    def __init__(self, transport, send, sleep=asyncio.sleep):
        self.transport, self.send, self.sleep = transport, send, sleep
        self.owners = {}
        transport.listeners.append(self.observe)

    def observe(self, state):
        for connection, owner in list(self.owners.items()):
            if state.status != "active" or state.turn_id != owner.turn_id:
                self.owners.pop(connection)
                if owner.task:
                    owner.task.cancel()

    async def start(self, connection, turn_id):
        # The delivery acknowledgement identifies the accepted turn. Do not bind
        # a late acknowledgement to a replacement turn, or resurrect a fast one.
        if not turn_id or self.transport.state != TurnState("active", turn_id):
            return
        owner = self.owners.get(connection)
        if owner and owner.turn_id == turn_id:
            return
        await self.stop(connection)
        if self.transport.state != TurnState("active", turn_id):
            return
        owner = Owner(turn_id)
        self.owners[connection] = owner
        # Sending typing must not block delivery, session replacements or other chats.
        owner.task = asyncio.create_task(self.refresh(connection, owner))

    async def refresh(self, connection, owner):
        try:
            while self.owners.get(connection) is owner:
                if self.transport.state != TurnState("active", owner.turn_id):
                    return
                delay = 4
                try:
                    await self.send(connection)
                except BridgeError as exc:
                    log.warning("typing_refresh_failed code=%s", exc.code)
                    delay = max(4, min(60, exc.details.get("retry_after", 4)))
                except Exception:  # noqa: BLE001 - typing must never fail delivery
                    log.warning("typing_refresh_failed code=operation_failed")
                await self.sleep(delay)
                if await self.transport.verify_turn(owner.turn_id) != owner.turn_id:
                    return
        finally:
            if self.owners.get(connection) is owner:
                self.owners.pop(connection)

    async def stop(self, connection):
        owner = self.owners.pop(connection, None)
        if owner and owner.task:
            owner.task.cancel()
            await asyncio.gather(owner.task, return_exceptions=True)

    async def close(self):
        if self.observe in self.transport.listeners:
            self.transport.listeners.remove(self.observe)
        for connection in list(self.owners):
            await self.stop(connection)
