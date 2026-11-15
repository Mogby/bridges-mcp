import fcntl
import hashlib
import json
import os
from pathlib import Path

from .errors import BridgeError


class InstanceState:
    def __init__(self, root: Path, instance_id: str):
        self.root, self.instance_id = root, instance_id
        self.directory = root / instance_id
        self.sessions_path = self.directory / "sessions.json"
        self.locks = []

    def acquire(self, tokens=()):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        keys = ["instance-" + self.instance_id]
        keys += ["bot-" + hashlib.sha256(t.split(":", 1)[0].encode()).hexdigest() for t in tokens]
        try:
            for key in sorted(keys):
                fd = os.open(self.root / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise BridgeError(
                        "instance_conflict",
                        "This instance or bot already has a running bridge process.",
                    ) from None
                self.locks.append(fd)
        except BaseException:
            self.close()
            raise

    def read_sessions(self):
        """Read shared logical-session pointers and desired turn settings."""
        if not self.sessions_path.exists():
            if (self.directory / "focus.json").exists():
                raise BridgeError(
                    "migration_required", "Migrate legacy focus to sessions.json first."
                )
            return {}
        try:
            data = json.loads(self.sessions_path.read_text())
            if data["instance_id"] != self.instance_id or not isinstance(data["sessions"], dict):
                raise ValueError()
            for key, value in data["sessions"].items():
                if (
                    not isinstance(key, str)
                    or not key.strip()
                    or not isinstance(value, dict)
                    or "thread_id" not in value
                    or set(value) - {"thread_id", "model", "effort", "creation_pending"}
                    or (
                        value["thread_id"] is not None
                        and (not isinstance(value["thread_id"], str) or not value["thread_id"])
                    )
                    or any(
                        not isinstance(v, str) or not v
                        for k, v in value.items()
                        if k not in {"thread_id", "creation_pending"}
                    )
                    or ("creation_pending" in value and type(value["creation_pending"]) is not bool)
                ):
                    raise ValueError()
            return data["sessions"]
        except (ValueError, KeyError, TypeError):
            raise BridgeError(
                "invalid_sessions", "Saved shared-session state is invalid."
            ) from None

    def save_sessions(self, sessions):
        """Atomically persist all pointers before publishing them in memory."""
        temporary = self.sessions_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"instance_id": self.instance_id, "sessions": sessions}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.sessions_path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        for fd in self.locks:
            os.close(fd)
        self.locks.clear()


def identity(meta):
    """Accept host metadata only; reject contradictory identity fields."""
    meta = meta or {}
    nested = meta.get("x-codex-turn-metadata", {})
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except ValueError:
            nested = {}
    if not isinstance(nested, dict):
        nested = {}
    values = [
        meta.get(k)
        for k in (
            "threadId",
            "thread_id",
            "openai/threadId",
            "openai/thread_id",
            "codexThreadId",
            "codex_thread_id",
        )
    ]
    values += [nested.get("thread_id"), nested.get("threadId")]
    if isinstance(meta.get("thread"), dict):
        values.append(meta["thread"].get("id"))
    ids = {v for v in values if isinstance(v, str) and v.strip()}
    if len(ids) != 1:
        raise BridgeError(
            "binding_conflict", "Host must supply one unambiguous calling chat identity."
        )
    turns = {
        v
        for v in (
            meta.get("turnId"),
            meta.get("turn_id"),
            nested.get("turn_id"),
            nested.get("turnId"),
        )
        if isinstance(v, str) and v
    }
    if len(turns) > 1:
        raise BridgeError("capability_unavailable", "Host supplied conflicting turn identities.")
    return ids.pop(), next(iter(turns), None)
