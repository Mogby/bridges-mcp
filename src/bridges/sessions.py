"""Harness-independent shared session routing and settings."""

import asyncio

from .errors import BridgeError, OperationRejected


class Sessions:
    def __init__(self, config, store, backends):
        self.config, self.store, self.backends = config, store, backends
        self.records = store.read_sessions()
        self.locks = {name: asyncio.Lock() for name in config.sessions}

    def record(self, name):
        return self.records.get(name, {"thread_id": None})

    def options(self, name):
        configured = self.config.sessions[name]
        defaults = {key: getattr(configured, key) for key in ("model", "effort")}
        return {
            key: self.record(name).get(key, default)
            for key, default in defaults.items()
            if self.record(name).get(key, default) is not None
        }

    def save(self, name, record):
        updated = {**self.records, name: record}
        self.store.save_sessions(updated)
        self.records = updated

    async def create(self, name):
        configured = self.config.sessions[name]
        previous = self.record(name)
        # A backend that is offline before submission has no uncertain mutation.
        await self.backends[configured.client].connect()
        # Write-ahead marker prevents duplicate automatic creation after a lost
        # acknowledgement, interrupted process, or failed final persistence.
        pending = {**previous, "creation_pending": True}
        self.save(name, pending)
        try:
            response = await self.backends[configured.client].create(
                configured.cwd, self.options(name)
            )
        except OperationRejected:
            self.save(
                name, {key: value for key, value in previous.items() if key != "creation_pending"}
            )
            raise
        thread_id = response["thread_id"]
        if not isinstance(thread_id, str) or not thread_id:
            raise BridgeError("invalid_payload", "The backend returned an invalid new session.")
        record = {"thread_id": thread_id}
        for key in ("model", "effort"):
            if response.get(key) is not None:
                if not isinstance(response[key], str) or not response[key]:
                    raise BridgeError("invalid_payload", "The backend returned invalid settings.")
                record[key] = response[key]
        self.save(name, record)
        return await self.backends[configured.client].session(thread_id)

    async def ensure(self, name):
        """Caller holds this logical session's lock for resolve and delivery."""
        configured, record = self.config.sessions[name], self.record(name)
        if record.get("creation_pending"):
            raise BridgeError(
                "session_creation_unknown",
                "Session creation has an uncertain outcome. Check backend state before using /new_session.",
            )
        if record["thread_id"] is None:
            return await self.create(name)
        backend = self.backends[configured.client]
        try:
            session = await backend.load(record["thread_id"], configured.cwd, self.options(name))
        except BridgeError as exc:
            if exc.code != "thread_missing":
                raise
            return await self.create(name)
        settings = getattr(session, "settings", {})
        if not record.get("model") and settings.get("model"):
            self.save(name, {**record, **settings, **self.options(name)})
        return session

    async def command(self, name, command, argument):
        await self.ensure(name)
        configured = self.config.sessions[name]
        models = await self.backends[configured.client].models()
        options = self.options(name)
        if command == "model":
            if not argument:
                choices = "\n".join(model.id for model in models)
                return (
                    f"Current model: {options.get('model', 'backend default')}\n"
                    f"Available models:\n{choices}\nUse /model <model>."
                )[:4096]
            selected = next(
                (model for model in models if argument in {model.id, *model.aliases}), None
            )
            if selected is None:
                raise BridgeError(
                    "invalid_model", "Unknown model. Use /model to see available models."
                )
            updated = {**options, "model": selected.id}
            efforts = set(selected.efforts)
            if updated.get("effort") not in efforts:
                if selected.default_effort is None:
                    updated.pop("effort", None)
                else:
                    updated["effort"] = selected.default_effort
        else:
            selected = next((model for model in models if model.id == options.get("model")), None)
            if selected is None:
                raise BridgeError("invalid_model", "Choose an available model with /model first.")
            efforts = list(selected.efforts)
            if not argument:
                return (
                    f"Current thinking: {options.get('effort', selected.default_effort)}"
                    f"\nAvailable levels: {', '.join(efforts)}\nUse /thinking <level>."
                )
            if argument not in efforts:
                raise BridgeError(
                    "invalid_effort", "Unsupported thinking level. Use /thinking for choices."
                )
            updated = {**options, "effort": argument}
        self.save(name, {**self.record(name), **updated})
        return (
            f"Model: {updated['model']}\nThinking: {updated.get('effort', 'default')}"
            "\nApplies on the next turn for every connection sharing this session."
        )
