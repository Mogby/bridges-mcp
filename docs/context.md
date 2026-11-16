# Context usage

The `/context` command shows cached context usage and observed compactions:

```text
Context usage: 80% (80,000/100,000 tokens).
2 compactions observed since tracking began.
Last reported at 2026-11-16T00:20:38+00:00
```

The command does not start a model request. It uses the normal admin permissions
and the connection's current logical session. Subagent usage is never added to its
parent's reading. Ordinary outgoing messages contain no context usage display.

## Meaning and freshness

The Codex adapter reads `thread/tokenUsage/updated`: the numerator is
`tokenUsage.last.totalTokens`, and the denominator is `modelContextWindow`. The
cumulative `total` is not context occupancy. The reported numerator includes the
latest request's input and output tokens. It estimates occupancy at that request;
new messages and tool results can change the next input. This is not an exact
live token counter or a promise about the compaction threshold. Unknown or invalid
limits never produce a guessed percentage.

`/context` takes a snapshot when the command runs. It reports unavailable before
the first usable sample and labels persisted readings stale after a process restart
or backend disconnect. During and immediately after compaction, the previous
reading stays stale until a new usage event arrives. The timestamp is the stored
UTC ISO time of the usage observation, not the time of the command. Stale readings
append `(Stale: awaiting update.)` to the timestamp line. If no sample exists,
the display reports unavailable and omits the timestamp line.

Thread read/resume metadata does not contain a usage snapshot. A restarted bridge
can show unavailable or stale information until normal incoming delivery resumes
the thread's event subscription and a new usage event arrives. `/context` itself
does not resume a thread or trigger inference. A new thread never inherits another
thread's usage or count.

## Compactions

Count only completed `contextCompaction` items. Persist each item ID and count it
once, even if start/completion events or completed history are replayed. The legacy
`thread/compacted` notification is not also counted. Readings can decrease after a
compaction.

Counts describe completed compactions observed since tracking began. Bridges does
not reconstruct earlier history or guarantee events during downtime were observed.
Persisted counts survive restarts, but must not be presented as lifetime totals.

## Delivery and persistence

Ordinary `telegram_send_text` calls send the caller's MarkdownV2 text unchanged.
The existing 4096-character limit and validation still apply.

See [[deployment#Context tracking storage|deployment storage]] for `context.sqlite`.
SQLite failures make tracking unavailable and emit a sanitized local warning;
they do not cause a successful Telegram send to be retried.
