"""Persist last-reported context usage and observed compactions per backend/thread."""

import logging
import os
import re
import sqlite3
from datetime import UTC, datetime

log = logging.getLogger(__name__)
MAX_INTEGER = (1 << 63) - 1


def integer(value, *, positive=False):
    return type(value) is int and (1 if positive else 0) <= value <= MAX_INTEGER


def escape_markdown(text):
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)


class ContextStore:
    def __init__(self, path):
        self.fresh = set()
        self.compacting = set()
        self.failed = False
        self.db = sqlite3.connect(path, timeout=2)
        os.chmod(path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS context_usage (
                client TEXT NOT NULL,
                thread TEXT NOT NULL,
                used INTEGER,
                window INTEGER,
                reported_at TEXT,
                PRIMARY KEY (client, thread)
            );
            CREATE TABLE IF NOT EXISTS context_compactions (
                client TEXT NOT NULL,
                thread TEXT NOT NULL,
                item TEXT NOT NULL,
                PRIMARY KEY (client, thread, item)
            );
        """)
        self.db.commit()

    def tracker(self, client):
        return ContextTracker(self, client)

    def display(self, client, thread):
        if self.failed:
            return "Context usage: unavailable.\nCompaction tracking unavailable."
        try:
            row = self.db.execute(
                "SELECT used, window, reported_at FROM context_usage WHERE client=? AND thread=?",
                (client, thread),
            ).fetchone()
            count = self.db.execute(
                "SELECT COUNT(*) FROM context_compactions WHERE client=? AND thread=?",
                (client, thread),
            ).fetchone()[0]
        except sqlite3.Error:
            self.failure()
            return "Context usage: unavailable.\nCompaction tracking unavailable."
        reported = None
        if row is None or row[0] is None:
            usage = "Context usage: unavailable (awaiting usage update)."
        else:
            used, window, reported_at = row
            if window is None:
                usage = f"Context usage: unavailable ({used:,} tokens; limit unavailable)."
            else:
                percent = (used * 100 + window // 2) // window
                usage = f"Context usage: {percent}% ({used:,}/{window:,} tokens)."
            reported = f"Last reported at {reported_at}"
            if (client, thread) not in self.fresh:
                reported += " (Stale: awaiting update.)"
        noun = "compaction" if count == 1 else "compactions"
        lines = [usage, f"{count:,} {noun} observed since tracking began."]
        if reported is not None:
            lines.append(reported)
        return "\n".join(lines)

    def failure(self):
        self.failed = True
        self.fresh.clear()
        log.warning("context_tracking_unavailable reason=storage_failure")

    def close(self):
        self.db.close()


class ContextTracker:
    """Adapter-facing normalized observations for one authenticated backend."""

    def __init__(self, store, client):
        self.store, self.client = store, client

    def usage(self, thread, used, window):
        if not integer(used) or (window is not None and not integer(window, positive=True)):
            self.store.fresh.discard((self.client, thread))
            return
        if self.store.failed:
            return
        try:
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO context_usage VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(client, thread) DO UPDATE SET used=excluded.used, "
                    "window=excluded.window, reported_at=excluded.reported_at",
                    (
                        self.client,
                        thread,
                        used,
                        window,
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    ),
                )
            key = (self.client, thread)
            if key not in self.store.compacting:
                self.store.fresh.add(key)
        except sqlite3.Error:
            self.store.failure()

    def compaction(self, thread, item, *, completed):
        if not isinstance(item, str) or not item:
            return
        key = (self.client, thread)
        if self.store.failed:
            return
        try:
            # Replayed completions must neither count twice nor invalidate newer usage.
            known = self.store.db.execute(
                "SELECT 1 FROM context_compactions WHERE client=? AND thread=? AND item=?",
                (self.client, thread, item),
            ).fetchone()
            if known:
                return
            self.store.fresh.discard(key)
            if completed:
                with self.store.db:
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO context_compactions VALUES (?, ?, ?)",
                        (self.client, thread, item),
                    )
                self.store.compacting.discard(key)
            else:
                self.store.compacting.add(key)
        except sqlite3.Error:
            self.store.failure()

    def disconnect(self):
        self.store.fresh = {key for key in self.store.fresh if key[0] != self.client}
        self.store.compacting = {key for key in self.store.compacting if key[0] != self.client}
