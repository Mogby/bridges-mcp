"""Optional application-side control socket; no Telegram credentials required.

The owning application supplies its initialized App Server client, thread/start
defaults and startup prompt. See docs/admin.md for the wire contract and usage.
"""

import asyncio
import json
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path

log = logging.getLogger(__name__)


class SessionControl:
    def __init__(self, call, state_path, defaults, startup_prompt=None):
        self.call = call
        self.state_path = Path(state_path)
        self.defaults = defaults
        self.startup_prompt = startup_prompt
        self.lock = asyncio.Lock()
        self.sessions = {}
        self.current = None
        self.server = None
        self.tasks = set()

    def save(self, current, sessions):
        data = {"thread_id": current, "sessions": sessions}
        fd, name = tempfile.mkstemp(prefix=".session-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
            directory = os.open(self.state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
        self.current, self.sessions = current, sessions

    def loaded(self, response):
        """Register the thread created/resumed during normal application startup."""
        sessions = {}
        if self.state_path.exists():
            sessions = json.loads(self.state_path.read_text()).get("sessions", {})
        options = {"model": response["model"]}
        if response.get("reasoningEffort"):
            options["effort"] = response["reasoningEffort"]
        thread = response["thread"]["id"]
        # Desired session settings survive restart, even before the next turn.
        sessions[thread] = {**options, **sessions.get(thread, {})}
        self.save(thread, sessions)

    async def models(self):
        result, cursor = [], None
        for _ in range(100):
            page = await self.call("model/list", {"cursor": cursor, "limit": 100})
            result.extend(model for model in page["data"] if not model.get("hidden"))
            cursor = page.get("nextCursor")
            if not cursor:
                return result
        raise ValueError("Model catalog pagination exceeded limit")

    async def execute(self, request):
        """Return (response, newly created thread requiring initialization)."""
        if not isinstance(request, dict) or set(request) != {
            "connection_id",
            "thread_id",
            "command",
            "argument",
        }:
            return {"error": "Invalid application control request."}, None
        thread, command, argument = (request["thread_id"], request["command"], request["argument"])
        if not all(isinstance(value, str) for value in (thread, command, argument)):
            return {"error": "Invalid application control request."}, None
        if thread not in self.sessions:
            return {"error": "This session is not managed by the application."}, None
        options = self.sessions[thread]
        response = {"thread_id": thread, "message": "Session settings."}
        if command == "turn_settings":
            return {**response, "turn_options": options}, None
        if command == "new_session":
            if argument:
                return {"error": "Usage: /new_session"}, None
            created = await self.call("thread/start", self.defaults)
            new_thread = created["thread"]["id"]
            new_options = {"model": created["model"]}
            if created.get("reasoningEffort"):
                new_options["effort"] = created["reasoningEffort"]
            self.save(new_thread, {**self.sessions, new_thread: new_options})
            return {
                "thread_id": new_thread,
                "message": "New session ready. Your previous conversation is saved.",
            }, new_thread
        if command not in {"model", "thinking"}:
            return {"error": "Unsupported application control command."}, None
        models = await self.models()
        selected = next((model for model in models if model["model"] == options["model"]), None)
        if command == "model":
            if not argument:
                choices = "\n".join(model["model"] for model in models)
                response["message"] = (
                    f"Current model: {options['model']}\nAvailable models:\n{choices}"
                    "\nUse /model <model>."
                )[:4096]
                return response, None
            selected = next(
                (model for model in models if argument in {model["model"], model["id"]}), None
            )
            if selected is None:
                return {"error": "Unknown model. Use /model to see available models."}, None
            updated = {**options, "model": selected["model"]}
            efforts = {item["reasoningEffort"] for item in selected["supportedReasoningEfforts"]}
            if updated.get("effort") not in efforts:
                updated["effort"] = selected["defaultReasoningEffort"]
        else:
            if selected is None:
                return {"error": "Current model is unavailable. Choose a model with /model."}, None
            efforts = [item["reasoningEffort"] for item in selected["supportedReasoningEfforts"]]
            if not argument:
                response["message"] = (
                    f"Current thinking: {options.get('effort', selected['defaultReasoningEffort'])}"
                    f"\nAvailable levels: {', '.join(efforts)}\nUse /thinking <level>."
                )
                return response, None
            if argument not in efforts:
                return {
                    "error": "Unsupported thinking level. Use /thinking to see available levels."
                }, None
            updated = {**options, "effort": argument}
        self.save(self.current, {**self.sessions, thread: updated})
        response["message"] = (
            f"Model: {updated['model']}\nThinking: {updated.get('effort', 'default')}"
            "\nApplies on the next turn in this session."
        )
        return response, None

    async def initialize(self, thread):
        if self.startup_prompt:
            try:
                await self.call(
                    "turn/start",
                    {
                        "threadId": thread,
                        **self.sessions[thread],
                        "input": [
                            {"type": "text", "text": self.startup_prompt, "text_elements": []}
                        ],
                    },
                )
            except Exception:  # noqa: BLE001 - sanitize application failures
                log.warning("admin_session_initialization_failed")

    async def handle(self, reader, writer):
        new_thread = None
        try:
            async with asyncio.timeout(12):
                request = json.loads(await reader.readline())
                async with self.lock:
                    response, new_thread = await self.execute(request)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except Exception:  # noqa: BLE001 - sanitize errors at the socket boundary
            log.warning("application_control_failed")
            with suppress(OSError):
                writer.write(
                    json.dumps(
                        {
                            "error": "Application operation failed; check session state before retrying."
                        }
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            if new_thread:
                task = asyncio.create_task(self.initialize(new_thread))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def start(self, path):
        # The application owns this private runtime directory and stale socket.
        path = Path(path)
        if path.exists() or path.is_symlink():
            if not path.is_socket():
                raise ValueError("Control path already exists and is not a socket")
            path.unlink()
        self.server = await asyncio.start_unix_server(self.handle, path=str(path), limit=65536)
        path.chmod(0o600)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
