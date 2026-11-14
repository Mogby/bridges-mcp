"""Multiplexed JSON-RPC over the application's existing Unix WebSocket."""

import asyncio
import json
import uuid

from websockets.exceptions import ConnectionClosed

from ..errors import BridgeError

MAX_FRAME = 16 * 1024 * 1024


class RPCRejected(BridgeError):
    def __init__(self, response):
        super().__init__("codex_rejected", "Codex explicitly rejected the request.")
        self.response = response


class RPC:
    def __init__(self, socket, *, on_event=lambda m: None, on_disconnect=lambda: None):
        self.socket = socket
        self.on_event, self.on_disconnect = on_event, on_disconnect
        self.pending = {}
        self.closed = False
        self.reader_task = asyncio.create_task(self.read_loop())

    async def send(self, message):
        data = json.dumps(message, separators=(",", ":"))
        if len(data.encode()) > MAX_FRAME:
            raise BridgeError("invalid_payload", "Codex request exceeds the frame limit.")
        try:
            await self.socket.send(data)
        except ConnectionClosed:
            raise ConnectionError("Codex connection lost") from None

    async def request(self, method, params, *, timeout=15):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                raise RPCRejected(response)
            return response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def read_loop(self):
        try:
            async for data in self.socket:
                message = json.loads(data)
                if not isinstance(message, dict):
                    raise TypeError("invalid message")
                future = self.pending.get(message.get("id"))
                if future is not None and "method" not in message:
                    if not future.done():
                        future.set_result(message)
                else:
                    self.on_event(message)
        except (OSError, ValueError, TypeError, KeyError, ConnectionClosed):
            pass
        finally:
            self.closed = True
            self.on_disconnect()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Codex connection lost"))

    async def close(self):
        self.reader_task.cancel()
        await asyncio.gather(self.reader_task, return_exceptions=True)
        await self.socket.close()
