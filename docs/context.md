# Context usage

Bridges appends a footer to outgoing `telegram_send_text` messages:

```text
Context usage: 80% (80,000/100,000 tokens). Last reported. 2 compactions observed since tracking began.
```

`/context` shows the same cached information without a model request. It uses the
normal admin permissions and the connection's current logical session. Text-tool
footers use the authenticated caller's backend and thread, even when the caller
explicitly sends to a different connection. Subagent usage is never added to its
parent's reading. Attachments and their captions are unchanged.

## Meaning and freshness

The Codex adapter reads `thread/tokenUsage/updated`: the numerator is
`tokenUsage.last.totalTokens`, and the denominator is `modelContextWindow`. The
cumulative `total` is not context occupancy. The reported numerator includes the
latest request's input and output tokens. It estimates occupancy at that request;
new messages and tool results can change the next input. This is not an exact
live token counter or a promise about the compaction threshold. Unknown or invalid
limits never produce a guessed percentage.

The footer takes a snapshot when the text is sent. Bridges does not edit the
message after later usage events. `/context` can show a newer reading. It reports
unavailable before the first usable sample and labels persisted readings stale
after a process restart or backend disconnect. During and immediately after
compaction, the previous reading stays stale until a new usage event arrives.

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

The caller's MarkdownV2 text stays unchanged; Bridges escapes only its own footer.
If the combined string exceeds the existing 4096-character validation limit,
Bridges sends the original valid text once and returns `context_footer_omitted:
true`. It does not truncate the caller's message or send a second notification.
Invalid original text still fails validation.

See [[deployment#Context tracking storage|deployment storage]] for `context.sqlite`.
SQLite failures make tracking unavailable and emit a sanitized local warning;
they do not cause a successful Telegram send to be retried.
