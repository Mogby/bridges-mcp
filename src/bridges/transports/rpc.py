"""Async request multiplexing; a lost response is never treated as rejection."""

import asyncio
import json
import struct
import uuid

from ..errors import BridgeError

MAX_FRAME = 16 * 1024 * 1024


class RPCRejected(BridgeError):
    def __init__(self, response):
        super().__init__("codex_rejected", "Codex explicitly rejected the request.")
        self.response = response  # Internal only: may contain user text.


class RPC:
    def __init__(
        self, reader, writer, *, framed=False, on_event=lambda m: None, on_disconnect=lambda: None
    ):
        self.reader, self.writer, self.framed = reader, writer, framed
        self.on_event, self.on_disconnect = on_event, on_disconnect
        self.client_id = "uninitialized"
        self.pending = {}
        self.write_lock = asyncio.Lock()
        self.reader_task = asyncio.create_task(self.read_loop())

    async def send(self, message):
        data = json.dumps(message, separators=(",", ":")).encode()
        if len(data) > MAX_FRAME:
            raise BridgeError("invalid_payload", "Codex request exceeds the frame limit.")
        async with self.write_lock:
            self.writer.write(struct.pack("<I", len(data)) + data if self.framed else data + b"\n")
            await self.writer.drain()

    async def request(self, method, params, *, target=None, version=1, timeout=15):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        if self.framed:
            message = {
                "type": "request",
                "requestId": request_id,
                "sourceClientId": self.client_id,
                "version": version,
                "method": method,
                "params": params,
                "timeoutMs": int(timeout * 1000),
            }
            if target:
                message["targetClientId"] = target
        else:
            message = {"id": request_id, "method": method, "params": params}
        try:
            await self.send(message)
            response = await asyncio.wait_for(future, timeout)
            if "error" in response or response.get("resultType", "success") != "success":
                raise RPCRejected(response)
            return response if self.framed else response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def broadcast(self, method, params, target=None, version=1):
        message = {
            "type": "broadcast",
            "sourceClientId": self.client_id,
            "method": method,
            "params": params,
            "version": version,
        }
        if target:
            message["targetClientIds"] = [target]
        await self.send(message)

    async def read_loop(self):
        try:
            while True:
                if self.framed:
                    (size,) = struct.unpack("<I", await self.reader.readexactly(4))
                    if size > MAX_FRAME:
                        raise ValueError("frame limit")
                    data = await self.reader.readexactly(size)
                else:
                    data = await self.reader.readline()
                    if not data:
                        break
                message = json.loads(data)
                if self.framed and message.get("type") == "client-discovery-request":
                    await self.send(
                        {
                            "type": "client-discovery-response",
                            "requestId": message["requestId"],
                            "response": {"canHandle": False},
                        }
                    )
                    continue
                request_id = message.get("requestId") if self.framed else message.get("id")
                future = self.pending.get(request_id)
                is_response = (
                    message.get("type") == "response" if self.framed else "method" not in message
                )
                if future is not None and is_response:
                    if not future.done():
                        future.set_result(message)
                else:
                    self.on_event(message)
        except (OSError, ValueError, asyncio.IncompleteReadError, KeyError):
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
