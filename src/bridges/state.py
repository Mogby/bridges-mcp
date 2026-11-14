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
        self.binding_path = self.directory / "binding.json"
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

    def read(self):
        if not self.binding_path.exists():
            return None
        try:
            data = json.loads(self.binding_path.read_text())
            if (
                data["instance_id"] != self.instance_id
                or data["mode"] != "app_server"
                or not isinstance(data["thread_id"], str)
                or not data["thread_id"]
            ):
                raise ValueError()
            return data["thread_id"]
        except (ValueError, KeyError):
            raise BridgeError(
                "binding_conflict",
                "Saved binding is invalid or is not an App Server binding; stop Bridges and use reset-binding.",
            ) from None

    def save(self, thread_id):
        previous = self.read()
        if previous and previous != thread_id:
            raise BridgeError("binding_conflict", "This instance is already bound to another chat.")
        temporary = self.binding_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {"instance_id": self.instance_id, "mode": "app_server", "thread_id": thread_id},
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.binding_path)

    def reset(self):
        self.binding_path.unlink(missing_ok=True)

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
