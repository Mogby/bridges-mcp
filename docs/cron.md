# Scheduled prompts

Jobs bind to a configured connection. Each occurrence goes into the session mapped
to that connection at delivery time. Connections sharing a logical session share
its bounded queue and normal automatic creation/replacement behavior. Cron does
not post the prompt to external chat; the agent may use messaging tools if asked.

## Tools

```json
{"name":"add_job","arguments":{"connection_id":"personal","name":"Weekday review","pattern":"0 9 * * 1-5","prompt":"Review today's priorities and send me a short summary."}}
{"name":"list_jobs","arguments":{"connection_id":"personal"}}
{"name":"get_job_prompt","arguments":{"id":"returned-job-id"}}
{"name":"pause_job","arguments":{"id":"returned-job-id"}}
{"name":"resume_job","arguments":{"id":"returned-job-id"}}
{"name":"delete_job","arguments":{"id":"returned-job-id"}}
```

IDs are server assigned. Names and prompts must be nonempty, up to 200 and 65,536
characters respectively. `add_job` accepts an optional IANA `timezone`, such as
`Asia/Tokyo`, to interpret that job’s expression in its own local time. Listing
returns only `id`, `name`, `pattern`, effective `timezone`, and `status`
(`active` or `paused`). Omit its filter to include all connections available to the
authenticated backend. Job access requires access to the connection's backend.

## Timing

Expressions have five fields: minute, hour, day of month, month, day of week.
Sunday is 0 or 7; named weekdays are supported. When both day fields are restricted,
either matching day is sufficient, as in Unix cron. APScheduler owns persistence
and execution; croniter calculates Unix-style trigger times.

```yaml
cron:
  timezone: Europe/Berlin
```

The configured timezone defaults to UTC and is independent of the system timezone.
Jobs without a `timezone` override inherit it, including existing jobs after an upgrade.
Changing `cron.timezone` applies to those jobs on restart. Explicit per-job zones remain
fixed across restarts and changes to the configured default. Invalid zone names are
rejected without creating a job.

For example, `0 9 * * *` with `timezone: Asia/Tokyo` runs at 09:00 in Tokyo, while
the same expression with `timezone: America/New_York` runs at 09:00 in New York.

Local schedules follow daylight-saving changes: missing local times may shift
forward and repeated times may occur twice. With the current croniter behavior,
Berlin `30 2 * * *` runs at 03:00 on the spring transition and at both occurrences
of 02:30 on the autumn transition. Use UTC for a fixed UTC schedule.

Paused jobs and downtime produce no catch-up backlog. During a running process,
late occurrences coalesce to the latest one, with up to 60 seconds of lateness.
Each job has at most one scheduler callback running at a time. Pausing/deleting
cannot retract a prompt already queued or accepted by the session.

## Persistence and delivery

`<state_dir>/cron.sqlite` stores jobs, prompts, paused status, and the last claimed
occurrence. A persisted claim suppresses repeated dispatch of that occurrence,
including after restart. Claims precede enqueueing: a crash in between can lose
that occurrence. Full queues, missing connections, and uncertain backend submissions
are not automatically retried. This is an at-most-once dispatch attempt, not a
guarantee of exactly-once agent execution or successful completion.

Scheduled input carries fenced YAML `bridge_context` with `bridge: cron`, connection
ID, job ID/name, pattern, effective timezone, UTC scheduled time, and stable occurrence
message ID. The original prompt follows unchanged. A leading slash does not invoke Telegram admin
commands. Jobs and paused status survive restarts; no external scheduler is needed.
