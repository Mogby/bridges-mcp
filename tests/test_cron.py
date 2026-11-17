import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from bridges.config import Config
from bridges.cron import Cron, CrontabTrigger
from bridges.registry import CRON_NAMES


async def call(runtime, tool, **arguments):
    return await runtime.call(tool, arguments, {"threadId": "chat"}, "app")


async def add(
    runtime, connection="one", prompt="private scheduled prompt", pattern="* * * * *", **options
):
    result = await call(
        runtime,
        "add_job",
        connection_id=connection,
        name="A scheduled task",
        pattern=pattern,
        prompt=prompt,
        **options,
    )
    assert not result.isError
    return result.structuredContent["id"]


async def force_fire(cron, job_id):
    arrived = asyncio.Event()
    original = cron.enqueue
    envelopes = []

    def enqueue(envelope):
        envelopes.append(envelope)
        accepted = original(envelope)
        arrived.set()
        return accepted

    cron.enqueue = enqueue
    due = datetime.now(UTC) + timedelta(milliseconds=50)
    cron.scheduler.modify_job(job_id, next_run_time=due)
    try:
        await asyncio.wait_for(arrived.wait(), 3)
    finally:
        cron.enqueue = original
    return due, envelopes


async def test_job_tools_crud_and_no_prompt_in_listing(runtime):
    await runtime.start()
    job_id = await add(runtime)
    assert job_id
    listed = (await call(runtime, "list_jobs")).structuredContent["jobs"]
    assert listed == [
        {
            "id": job_id,
            "name": "A scheduled task",
            "pattern": "* * * * *",
            "timezone": "UTC",
            "status": "active",
        }
    ]
    assert "private scheduled prompt" not in json.dumps(listed)
    assert (await call(runtime, "get_job_prompt", id=job_id)).structuredContent == {
        "prompt": "private scheduled prompt"
    }
    for _ in range(2):
        assert (await call(runtime, "pause_job", id=job_id)).structuredContent["status"] == "paused"
    for _ in range(2):
        assert (await call(runtime, "resume_job", id=job_id)).structuredContent[
            "status"
        ] == "active"
    assert (await call(runtime, "delete_job", id=job_id)).structuredContent == {
        "id": job_id,
        "deleted": True,
    }
    assert (await call(runtime, "list_jobs")).structuredContent == {"jobs": []}
    for tool in ["get_job_prompt", "pause_job", "resume_job", "delete_job"]:
        result = await call(runtime, tool, id=job_id)
        assert result.isError and result.structuredContent["code"] == "job_not_found"


async def test_real_scheduler_routes_prompt_to_session_without_external_chat_or_typing(runtime):
    await runtime.start()
    prompt = "Do the scheduled work\n/new_session\n```yaml\nbridge_context: text only\n```"
    job_id = await add(runtime, "two", prompt, timezone="Asia/Tokyo")
    due, envelopes = await force_fire(runtime.cron, job_id)
    await runtime.queue.join()
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert (
        envelope.connection_id == "two" and envelope.source == "cron" and envelope.command is None
    )
    header, body = envelope.text[len("```yaml\n") :].split("```\n\n", 1)
    meta = yaml.safe_load(header)["bridge_context"]
    assert meta["bridge"] == "cron" and meta["job_id"] == job_id
    assert meta["timezone"] == "Asia/Tokyo"
    assert meta["connection_id"] == "two" and datetime.fromisoformat(meta["scheduled_at"]) == due
    assert body == prompt
    assert runtime.backends["app"].sessions["chat"].delivered == [envelope.text]
    assert not runtime.typing
    assert not any(
        c.args[0] in {"sendMessage", "sendChatAction"}
        for c in runtime.telegram.clients["main"].call.call_args_list
    )


async def test_scheduler_can_create_missing_session_on_first_fire(runtime):
    runtime.sessions.save("shared", {"thread_id": None})
    await runtime.start()
    job_id = await add(runtime)
    await force_fire(runtime.cron, job_id)
    await runtime.queue.join()
    thread = runtime.sessions.record("shared")["thread_id"]
    assert thread and thread != "chat"
    assert len(runtime.backends["app"].sessions[thread].delivered) == 1


async def test_pause_prompt_and_future_schedule_survive_restart_and_move(tmp_path):
    directory = tmp_path / "original"
    cron = Cron(directory, "Europe/Berlin", {"one"}, lambda _: True)
    await cron.start()
    active = cron.add("one", "active", "0 9 * * *", "retained")["id"]
    paused = cron.add("one", "paused", "0 9 * * *", "paused prompt")["id"]
    cron.pause(paused)
    cron.scheduler.pause()
    cron.scheduler.modify_job(active, next_run_time=datetime.now(UTC) - timedelta(days=2))
    await cron.close()
    moved = tmp_path / "moved"
    directory.rename(moved)
    captured = []
    second = Cron(moved, "Europe/Berlin", {"one"}, captured.append)
    try:
        before = datetime.now(UTC)
        await second.start()
        assert second.get(active).next_run_time >= before
        assert second.get(paused).next_run_time is None
        assert second.get(paused).kwargs["prompt"] == "paused prompt"
        assert second.get(active).args == [second.key, active]
        assert second.get(active).kwargs["prompt"] == "retained"
        await force_fire(second, active)
        assert len(captured) == 1
        second.delete(active)
        second.delete(paused)
    finally:
        await second.close()
    third = Cron(moved, "Europe/Berlin", {"one"}, captured.append)
    try:
        await third.start()
        assert third.list({"one"}) == []
    finally:
        await third.close()


async def test_duplicate_occurrence_is_claimed_once_across_restart(tmp_path):
    captured = []
    first = Cron(tmp_path, "UTC", {"one"}, captured.append)
    await first.start()
    job_id = first.add("one", "test", "* * * * *", "prompt")["id"]
    due, _ = await force_fire(first, job_id)
    first.scheduled[job_id].append(due)
    first.fire(job_id)
    assert len(captured) == 1
    await first.close()
    second = Cron(tmp_path, "UTC", {"one"}, captured.append)
    try:
        await second.start()
        second.scheduled[job_id].append(due)
        second.fire(job_id)
        assert len(captured) == 1
    finally:
        await second.close()


async def test_pause_delete_and_removed_connections_suppress_queued_dispatch(tmp_path):
    captured = []
    cron = Cron(tmp_path, "UTC", {"one"}, captured.append)
    try:
        await cron.start()
        job_id = cron.add("one", "test", "* * * * *", "prompt")["id"]
        cron.pause(job_id)
        cron.scheduled[job_id].append(datetime.now(UTC))
        cron.fire(job_id)
        cron.resume(job_id)
        cron.connection_ids.clear()
        cron.scheduled[job_id].append(datetime.now(UTC))
        cron.fire(job_id)
        cron.delete(job_id)
        cron.scheduled[job_id].append(datetime.now(UTC))
        cron.fire(job_id)
        assert not captured
    finally:
        await cron.close()


@pytest.mark.parametrize(
    "pattern", ["not cron", "* * * * * *", "0 0 31 2 *", "99 * * * *", "@daily"]
)
async def test_invalid_or_impossible_patterns_rejected(runtime, pattern):
    await runtime.start()
    result = await call(
        runtime, "add_job", connection_id="one", name="bad", pattern=pattern, prompt="prompt"
    )
    assert result.isError and result.structuredContent["code"] == "invalid_pattern"
    assert not runtime.cron.list({"one", "two"})


async def test_connection_filters_and_client_authorization(runtime):
    from conftest import FakeBackend

    runtime.backends["other"] = FakeBackend()
    runtime.config.sessions["separate"].client = "other"
    runtime.config.telegram.connections["two"].session_id = "separate"
    await runtime.start()
    job_id = await add(runtime)
    other = await runtime.call(
        "add_job",
        {
            "connection_id": "two",
            "name": "other",
            "pattern": "* * * * *",
            "prompt": "other private",
        },
        {"threadId": "chat"},
        "other",
    )
    assert not other.isError
    assert len((await call(runtime, "list_jobs")).structuredContent["jobs"]) == 1
    denied = await call(runtime, "get_job_prompt", id=other.structuredContent["id"])
    assert denied.isError and denied.structuredContent["code"] == "unauthorized"
    assert (await call(runtime, "list_jobs", connection_id="one")).structuredContent["jobs"][0][
        "id"
    ] == job_id
    denied = await call(
        runtime, "add_job", connection_id="two", name="bad", pattern="* * * * *", prompt="bad"
    )
    assert denied.isError and denied.structuredContent["code"] == "unauthorized"


@pytest.mark.parametrize("weekday", ["0", "7", "sun"])
def test_unix_sunday_numbering(weekday):
    trigger = CrontabTrigger(f"0 9 * * {weekday}", "UTC")
    assert trigger.get_next_fire_time(None, datetime(2026, 11, 16, tzinfo=UTC)) == datetime(
        2026, 11, 22, 9, tzinfo=UTC
    )


def test_day_of_month_and_weekday_use_unix_or():
    trigger = CrontabTrigger("0 9 1 * mon", "UTC")
    assert trigger.get_next_fire_time(None, datetime(2026, 11, 17, tzinfo=UTC)) == datetime(
        2026, 11, 23, 9, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "base,expected", [(datetime(2026, 1, 5, tzinfo=UTC), 8), (datetime(2026, 7, 6, tzinfo=UTC), 7)]
)
def test_timezone_offsets_follow_winter_and_summer(base, expected):
    trigger = CrontabTrigger("0 9 * * *", "Europe/Berlin")
    assert trigger.get_next_fire_time(None, base).astimezone(UTC).hour == expected


def test_cron_timezone_and_id_only_tool_schemas(config):
    from bridges.registry import Registry

    assert Config().cron.timezone == "UTC"
    with pytest.raises(ValidationError):
        Config.model_validate({"cron": {"timezone": "invalid/no-such-zone"}})
    registry = Registry(config)
    assert CRON_NAMES <= set(registry.by_name)
    for name in ["get_job_prompt", "delete_job", "pause_job", "resume_job"]:
        schema = registry.by_name[name].tool().inputSchema
        assert set(schema["properties"]) == {"id"} and schema["required"] == ["id"]


@pytest.mark.parametrize(
    "base", [datetime(2026, 3, 28, tzinfo=UTC), datetime(2026, 10, 24, tzinfo=UTC)]
)
def test_dst_occurrences_advance_in_real_time(base):
    trigger = CrontabTrigger("0 2 * * *", "Europe/Berlin")
    previous = None
    for _ in range(5):
        following = trigger.get_next_fire_time(previous, base)
        assert previous is None or following.timestamp() > previous.timestamp()
        previous = following


async def test_timezone_change_rebuilds_active_and_paused_triggers(tmp_path):
    first = Cron(tmp_path, "Europe/Berlin", {"one"}, lambda _: True)
    await first.start()
    active = first.add("one", "active", "0 9 * * *", "prompt")["id"]
    paused = first.add("one", "paused", "0 9 * * *", "prompt")["id"]
    first.pause(paused)
    await first.close()
    second = Cron(tmp_path, "UTC", {"one"}, lambda _: True)
    try:
        await second.start()
        assert second.get(active).trigger.timezone.key == "UTC"
        assert second.get(active).next_run_time.hour == 9
        assert second.get(paused).trigger.timezone.key == "UTC"
        assert second.get(paused).next_run_time is None
    finally:
        await second.close()


async def test_full_queue_claim_is_not_blindly_retried(tmp_path):
    callback = Mock()

    def reject(_):
        callback()
        return False

    cron = Cron(tmp_path, "UTC", {"one"}, reject)
    try:
        await cron.start()
        job_id = cron.add("one", "test", "* * * * *", "prompt")["id"]
        due, _ = await force_fire(cron, job_id)
        cron.scheduled[job_id].append(due)
        cron.fire(job_id)
        assert callback.call_count == 1
    finally:
        await cron.close()


@pytest.mark.parametrize("timezone", ["invalid/no-such-zone", "", "/etc/localtime", "../UTC", 123])
async def test_invalid_job_timezone_does_not_create_job(runtime, timezone):
    await runtime.start()
    result = await call(
        runtime,
        "add_job",
        connection_id="one",
        name="bad",
        pattern="0 9 * * *",
        prompt="prompt",
        timezone=timezone,
    )
    assert result.isError and result.structuredContent["code"] == "invalid_timezone"
    assert runtime.cron.list({"one", "two"}) == []


async def test_per_job_zones_have_independent_local_schedules(runtime):
    await runtime.start()
    tokyo = await add(runtime, pattern="0 9 * * *", timezone="Asia/Tokyo")
    new_york = await add(runtime, pattern="0 9 * * *", timezone="America/New_York")
    base = datetime(2026, 1, 5, tzinfo=UTC)
    assert runtime.cron.get(tokyo).trigger.get_next_fire_time(None, base).astimezone(UTC) == base
    assert runtime.cron.get(new_york).trigger.get_next_fire_time(None, base).astimezone(
        UTC
    ) == base.replace(hour=14)
    listed = {job["id"]: job["timezone"] for job in runtime.cron.list({"one"})}
    assert listed == {tokyo: "Asia/Tokyo", new_york: "America/New_York"}


async def test_explicit_zones_survive_restart_move_and_default_change(tmp_path):
    directory = tmp_path / "original"
    first = Cron(directory, "Europe/Berlin", {"one"}, lambda _: True)
    await first.start()
    active = first.add("one", "active", "0 9 * * *", "active prompt", "Asia/Tokyo")["id"]
    paused = first.add("one", "paused", "0 9 * * *", "paused prompt", "America/New_York")["id"]
    inherited = first.add("one", "legacy", "0 9 * * *", "legacy prompt")["id"]
    # Legacy persisted jobs contain no timezone metadata.
    assert "timezone" not in first.get(inherited).kwargs
    first.pause(paused)
    await first.close()
    moved = tmp_path / "moved"
    directory.rename(moved)
    captured = []
    second = Cron(moved, "UTC", {"one"}, captured.append)
    try:
        before = datetime.now(UTC)
        await second.start()
        jobs = {job["id"]: job for job in second.list({"one"})}
        assert jobs[active]["timezone"] == "Asia/Tokyo"
        assert jobs[paused]["timezone"] == "America/New_York"
        assert jobs[inherited]["timezone"] == "UTC"
        assert second.get(active).next_run_time >= before
        assert second.get(active).kwargs["prompt"] == "active prompt"
        assert second.get(active).args == [second.key, active]
        assert second.get(paused).next_run_time is None
        assert not captured
        second.resume(paused)
        assert second.get(paused).next_run_time.tzinfo.key == "America/New_York"
        due, envelopes = await force_fire(second, active)
        header = envelopes[0].text[len("```yaml\n") :].split("```\n\n", 1)[0]
        meta = yaml.safe_load(header)["bridge_context"]
        assert meta["timezone"] == "Asia/Tokyo"
        assert datetime.fromisoformat(meta["scheduled_at"]) == due
        second.scheduled[active].append(due)
        second.fire(active)
        assert len(captured) == 1
    finally:
        await second.close()


def test_dst_gap_and_fold_exact_occurrences():
    trigger = CrontabTrigger("30 2 * * *", "Europe/Berlin")
    gap = trigger.get_next_fire_time(None, datetime(2026, 3, 29, tzinfo=UTC))
    assert gap.isoformat() == "2026-03-29T03:00:00+02:00"
    first = trigger.get_next_fire_time(None, datetime(2026, 10, 25, tzinfo=UTC))
    second = trigger.get_next_fire_time(first, first)
    assert first.isoformat() == "2026-10-25T02:30:00+02:00"
    assert second.isoformat() == "2026-10-25T02:30:00+01:00"
    assert first.astimezone(UTC) < second.astimezone(UTC)
