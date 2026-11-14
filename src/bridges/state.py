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
        self.focus_path = self.directory / "focus.json"
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

    def read_focus(self):
        """Only routing is persisted; applications own their conversation state."""
        if not self.focus_path.exists():
            return {}
        try:
            data = json.loads(self.focus_path.read_text())
            if (
                data["instance_id"] != self.instance_id
                or not isinstance(data["connections"], dict)
                or any(
                    not isinstance(key, str)
                    or not key
                    or not isinstance(value, dict)
                    or set(value) != {"client_id", "thread_id"}
                    or any(not isinstance(v, str) or not v for v in value.values())
                    for key, value in data["connections"].items()
                )
            ):
                raise ValueError()
            return data["connections"]
        except (ValueError, KeyError, TypeError):
            raise BridgeError("invalid_focus", "Saved connection focus is invalid.") from None

    def save_focus(self, connections):
        """Replace the complete map atomically under the instance's process lock."""
        temporary = self.focus_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"instance_id": self.instance_id, "connections": connections}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.focus_path)
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

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
