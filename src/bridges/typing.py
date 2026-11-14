import asyncio
import logging
from dataclasses import dataclass

from .errors import BridgeError

log = logging.getLogger(__name__)


@dataclass
class Owner:
    turn_id: str
    task: asyncio.Task | None = None


class TypingManager:
    def __init__(self, transport, send, sleep=asyncio.sleep):
        self.transport, self.send, self.sleep = transport, send, sleep
        self.owners = {}
        self.lock = asyncio.Lock()
        transport.listeners.append(self.observe)

    def observe(self, state):
        for connection, owner in list(self.owners.items()):
            if state.status != "active" or state.turn_id != owner.turn_id:
                self.owners.pop(connection)
                if owner.task:
                    owner.task.cancel()

    async def set(self, connection, enabled, claimed_turn=None):
        async with self.lock:
            if not enabled:
                await self.stop(connection)
                return {"enabled": False}
            turn = await self.transport.verify_turn(claimed_turn)
            if not turn:
                raise BridgeError(
                    "capability_unavailable",
                    "Cannot verify and observe the calling active turn; typing was not started.",
                )
            owner = self.owners.get(connection)
            if owner and owner.turn_id == turn:
                return {"enabled": True, "turn_id": turn}
            await self.stop(connection)
            owner = Owner(turn)
            self.owners[connection] = owner
            try:
                await self.send(connection)
            except BaseException:
                if self.owners.get(connection) is owner:
                    self.owners.pop(connection)
                raise
            if self.owners.get(connection) is not owner:
                raise BridgeError(
                    "capability_unavailable",
                    "The owning turn ended while typing was being enabled.",
                )
            owner.task = asyncio.create_task(self.refresh(connection, owner))
            return {"enabled": True, "turn_id": turn}

    async def refresh(self, connection, owner):
        delay = 4
        try:
            while True:
                await self.sleep(delay)
                turn = await self.transport.verify_turn(owner.turn_id)
                if turn != owner.turn_id or self.owners.get(connection) is not owner:
                    return
                try:
                    await self.send(connection)
                    delay = 4
                except BridgeError as exc:
                    log.warning("typing_refresh_failed code=%s", exc.code)
                    delay = max(4, min(60, exc.details.get("retry_after", 4)))
        finally:
            if self.owners.get(connection) is owner:
                self.owners.pop(connection)

    async def stop(self, connection):
        owner = self.owners.pop(connection, None)
        if owner and owner.task:
            owner.task.cancel()
            await asyncio.gather(owner.task, return_exceptions=True)

    async def close(self):
        self.transport.listeners.remove(self.observe)
        for connection in list(self.owners):
            await self.stop(connection)
