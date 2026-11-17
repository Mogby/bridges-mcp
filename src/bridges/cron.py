"""Persistent scheduling into sessions, independent of the OS and harness."""

import asyncio
import logging
import sqlite3
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    EVENT_SCHEDULER_SHUTDOWN,
)
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from sqlalchemy.engine import URL

from .envelope import Inbound
from .errors import BridgeError

log = logging.getLogger(__name__)
_SERVICES = {}


class CrontabTrigger(BaseTrigger):
    """Delegate Unix five-field semantics, including Sunday=0 and day OR, to croniter.

    APScheduler 3's native cron trigger uses different weekday/day semantics.
    APScheduler still owns timing, persistence, execution, and job lifecycle.
    """

    def __init__(self, pattern, timezone):
        if not isinstance(pattern, str) or len(pattern) > 256 or len(pattern.split()) != 5:
            raise ValueError("Use a five-field cron expression")
        self.pattern = pattern
        self.timezone = ZoneInfo(timezone)
        self.get_next_fire_time(None, datetime.now(UTC))

    def get_next_fire_time(self, previous_fire_time, now):
        base = (
            previous_fire_time
            if previous_fire_time is not None
            else now - timedelta(microseconds=1)
        )
        return croniter(
            self.pattern, base.astimezone(self.timezone), max_years_between_matches=8
        ).get_next(datetime)

    def __str__(self):
        return f"crontab[{self.pattern}; {self.timezone.key}]"


async def dispatch(store_id, job_id, **_metadata):
    """Importable persistent callable; never serialize a Runtime or bound method."""
    service = _SERVICES.get(store_id)
    if service is not None:
        service.fire(job_id)


class Cron:
    def __init__(self, state_dir: Path, timezone, connection_ids, enqueue):
        self.directory = state_dir
        self.key = str(state_dir.resolve())
        self.timezone = timezone
        self.connection_ids, self.enqueue = set(connection_ids), enqueue
        self.path = state_dir / "cron.sqlite"
        self.scheduler = None
        self.claims = None
        self.scheduled = defaultdict(deque)

    async def start(self):
        if self.scheduler is not None:
            return
        if self.key in _SERVICES:
            raise BridgeError("state_conflict", "A scheduler already owns this state directory.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.claims = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.claims.execute(
            "CREATE TABLE IF NOT EXISTS cron_claims "
            "(job_id TEXT PRIMARY KEY, scheduled_at TEXT NOT NULL)"
        )
        self.claims.commit()
        self.scheduler = AsyncIOScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(url=URL.create("sqlite", database=str(self.path)))
            },
            timezone=ZoneInfo(self.timezone),
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
        )
        self.scheduler.add_listener(
            self.event, EVENT_JOB_SUBMITTED | EVENT_JOB_MISSED | EVENT_JOB_ERROR
        )
        _SERVICES[self.key] = self
        self.scheduler.start(paused=True)
        # Persisted jobs survive restarts, but downtime is not a backlog. Repair
        # the callback key too, so a moved state directory remains portable.
        now = datetime.now(UTC)
        for job in self.scheduler.get_jobs():
            trigger = CrontabTrigger(
                job.kwargs["pattern"], job.kwargs.get("timezone") or self.timezone
            )
            changes = {"args": [self.key, job.id], "trigger": trigger}
            if job.next_run_time is not None:
                changes["next_run_time"] = trigger.get_next_fire_time(None, now)
            self.scheduler.modify_job(job.id, **changes)
        self.scheduler.resume()

    def event(self, event):
        if event.code == EVENT_JOB_SUBMITTED:
            self.scheduled[event.job_id].extend(event.scheduled_run_times)
        else:
            times = self.scheduled.get(event.job_id)
            if times and event.scheduled_run_time in times:
                times.remove(event.scheduled_run_time)
                if not times:
                    self.scheduled.pop(event.job_id, None)

    def get(self, job_id):
        job = self.scheduler.get_job(job_id)
        if job is None:
            raise BridgeError("job_not_found", "The scheduled job does not exist.")
        return job

    def add(self, connection_id, name, pattern, prompt, timezone=None):
        if connection_id not in self.connection_ids:
            raise BridgeError("unknown_connection", "The connection_id is not configured.")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise BridgeError("invalid_payload", "Job name must contain 1–200 characters.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 65536:
            raise BridgeError("invalid_payload", "Job prompt must contain 1–65536 characters.")
        if timezone is not None:
            try:
                if not isinstance(timezone, str) or not timezone:
                    raise ValueError
                ZoneInfo(timezone)
            except (ValueError, KeyError):
                raise BridgeError("invalid_timezone", "Use a valid IANA job timezone.") from None
        try:
            trigger = CrontabTrigger(pattern, timezone or self.timezone)
        except (ValueError, KeyError, CroniterBadCronError, CroniterBadDateError):
            raise BridgeError(
                "invalid_pattern", "Use a valid, satisfiable five-field cron expression."
            ) from None
        job_id = uuid4().hex
        metadata = {"connection_id": connection_id, "pattern": pattern, "prompt": prompt}
        if timezone is not None:
            metadata["timezone"] = timezone
        self.scheduler.add_job(
            dispatch,
            trigger=trigger,
            id=job_id,
            name=name,
            args=[self.key, job_id],
            kwargs=metadata,
        )
        return {"id": job_id}

    def list(self, connection_ids):
        return [
            {
                "id": job.id,
                "name": job.name,
                "pattern": job.kwargs["pattern"],
                "timezone": job.trigger.timezone.key,
                "status": "paused" if job.next_run_time is None else "active",
            }
            for job in self.scheduler.get_jobs()
            if job.kwargs["connection_id"] in connection_ids
        ]

    def pause(self, job_id):
        self.get(job_id)
        self.scheduler.pause_job(job_id)
        return {"id": job_id, "status": "paused"}

    def resume(self, job_id):
        job = self.get(job_id)
        if job.next_run_time is None:
            self.scheduler.resume_job(job_id)
        return {"id": job_id, "status": "active"}

    def delete(self, job_id):
        try:
            self.scheduler.remove_job(job_id)
        except JobLookupError:
            raise BridgeError("job_not_found", "The scheduled job does not exist.") from None
        with self.claims:
            self.claims.execute("DELETE FROM cron_claims WHERE job_id = ?", (job_id,))
        return {"id": job_id, "deleted": True}

    def fire(self, job_id):
        times = self.scheduled.get(job_id)
        if not times:
            return
        scheduled_at = times.popleft().astimezone(UTC)
        if not times:
            self.scheduled.pop(job_id, None)
        job = self.scheduler.get_job(job_id)
        if job is None or job.next_run_time is None:
            return
        connection_id = job.kwargs["connection_id"]
        if connection_id not in self.connection_ids:
            log.warning("cron_delivery_dropped code=unknown_connection")
            return
        occurrence = scheduled_at.isoformat()
        with self.claims:
            claim = self.claims.execute(
                "INSERT INTO cron_claims (job_id, scheduled_at) VALUES (?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET scheduled_at = excluded.scheduled_at "
                "WHERE cron_claims.scheduled_at < excluded.scheduled_at",
                (job_id, occurrence),
            )
        if claim.rowcount != 1:
            return
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = {
            "bridge": "cron",
            "connection_id": connection_id,
            "message_id": f"cron:{job_id}:{occurrence}",
            "received_at": now,
            "job_id": job_id,
            "job_name": job.name,
            "pattern": job.kwargs["pattern"],
            "timezone": job.trigger.timezone.key,
            "scheduled_at": occurrence,
        }
        text = (
            "```yaml\n"
            + yaml.safe_dump({"bridge_context": header}, sort_keys=False, allow_unicode=True)
            + "```\n\n"
            + job.kwargs["prompt"]
        )
        try:
            accepted = self.enqueue(Inbound(connection_id, text, source="cron"))
            if accepted is False:
                log.warning("cron_delivery_dropped code=buffer_full")
        except Exception:  # noqa: BLE001 - uncertain dispatch is never retried
            log.warning("cron_delivery_dropped code=dispatch_failed")

    async def close(self):
        if self.scheduler is not None and self.scheduler.running:
            stopped = asyncio.get_running_loop().create_future()
            self.scheduler.add_listener(
                lambda _event: stopped.set_result(None) if not stopped.done() else None,
                EVENT_SCHEDULER_SHUTDOWN,
            )
            self.scheduler.shutdown(wait=False)
            await stopped
        if _SERVICES.get(self.key) is self:
            _SERVICES.pop(self.key)
        if self.claims is not None:
            self.claims.close()
            self.claims = None
        self.scheduler = None
        self.scheduled.clear()
