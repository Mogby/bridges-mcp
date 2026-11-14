"""Async request multiplexing; a lost response is never treated as rejection."""

import asyncio
import json
import uuid

from ..errors import BridgeError

MAX_FRAME = 16 * 1024 * 1024


class RPCRejected(BridgeError):
    def __init__(self, response):
        super().__init__("codex_rejected", "Codex explicitly rejected the request.")
        self.response = response  # Internal only: may contain user text.


class RPC:
    def __init__(self, reader, writer, *, on_event=lambda m: None, on_disconnect=lambda: None):
        self.reader, self.writer = reader, writer
        self.on_event, self.on_disconnect = on_event, on_disconnect
        self.pending = {}
        self.write_lock = asyncio.Lock()
        self.reader_task = asyncio.create_task(self.read_loop())

    async def send(self, message):
        data = json.dumps(message, separators=(",", ":")).encode()
        if len(data) > MAX_FRAME:
            raise BridgeError("invalid_payload", "Codex request exceeds the frame limit.")
        async with self.write_lock:
            self.writer.write(data + b"\n")
            await self.writer.drain()

    async def request(self, method, params, *, timeout=15):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message = {"id": request_id, "method": method, "params": params}
        try:
            await self.send(message)
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                raise RPCRejected(response)
            return response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def read_loop(self):
        try:
            while True:
                data = await self.reader.readline()
                if not data:
                    break
                if len(data) > MAX_FRAME:
                    raise ValueError("frame limit")
                message = json.loads(data)
                if not isinstance(message, dict):
                    raise TypeError("invalid message")
                future = self.pending.get(message.get("id"))
                is_response = "method" not in message
                if future is not None and is_response:
                    if not future.done():
                        future.set_result(message)
                else:
                    self.on_event(message)
        except (OSError, ValueError, TypeError, asyncio.IncompleteReadError, KeyError):
            pass
        finally:
            self.on_disconnect()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Codex lifecycle connection lost"))

    async def close(self):
        self.reader_task.cancel()
        await asyncio.gather(self.reader_task, return_exceptions=True)
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (OSError, RuntimeError):
            pass
