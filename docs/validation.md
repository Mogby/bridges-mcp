# Validation record

Implementation and local validation: 2026-11-14. Desktop-bundled Codex version:
`0.169.0-alpha.11.1`. The generated installed App Server JSON schema was inspected
for initialization, thread read/resume/turn pagination, start/steer, and lifecycle
notification fields. Schemas and extracted app bundles are not shipped here.

## Automated checks

53 standard tests passed, plus the opt-in installed-App-Server check. Ruff lint
and formatting checks passed. The package built and its installed CLI entry point
was exercised.

The pytest suite exercises strict config/routing validation, token redaction,
instance and bot locks, persistent binding, metadata conflicts, disabled tool
visibility, every inbound media mapping, ignored update kinds, handle expiry and
connection scope, bounded bursts, one poller per bot, offsets and duplicates,
webhook conflict, payload limits, partial sends, ambiguous sends without retry,
download metadata and paths, and MCP initialization/discovery/tool calls.

A fake clock exercises typing across refresh intervals, idempotent enable/disable,
two connections, cancellation during an in-flight action, turn replacement,
completion/interruption, disconnect, and outbound sends. Transport fixtures cover
snapshot/patch application, revision gaps, unknown submission outcomes, a definite
turn-boundary rejection with one bounded retry, stale completion events, and
approval/input states remaining active without being answered.

MCP tests use the official SDK in memory and a real stdio child process. Network
calls in normal tests are mocked; the optional installed-App-Server check uses an
isolated unauthenticated home and makes no model request.

## Live desktop / Telegram smoke test

The user supplied a test bot token locally and sent a message to the bot. The test
harness called the real MCP server through an SDK client, with this chat selected
explicitly by test-host metadata. It did not infer a target from recency or cwd.

Observed results:

- The desktop owner supplied an active-turn snapshot with an authoritative turn ID.
- The Telegram poller received the user's test message and desktop IPC acknowledged
  steering. The message then appeared as a user message in this same active chat.
- `get_protocol_docs`, all three send tools, both download tools, and typing
  enable/disable returned successful MCP results.
- A sent image was downloaded and returned as MCP image content plus local metadata.
- A sent file was downloaded and its bytes matched the original fixture.
- Three Telegram typing actions were acknowledged over two refresh intervals.
- Outbound messages did not stop typing. The test explicitly disabled typing and
  closed the listener, SDK session, HTTP client, and desktop subscription.

The user then rotated the bot token. Tokens, private chat identifiers, local
runtime artifacts, and downloaded files are excluded from version control.

After the first live test, the user requested a presentation change: the metadata
header is now a fenced YAML code block, with no `---` separator. Tests and the
design document use this revised format.

## Remaining acceptance boundaries

- Loaded desktop idle wake-up was verified in an earlier desktop delivery check,
  not repeated by interrupting the current implementation chat. A state-check/queue
  race is tested as a distinct delivery branch and documented; immediate steering is not
  guaranteed if a different actor starts a turn in that narrow window.
- Completion/failure/interruption cancellation is covered by deterministic
  lifecycle tests. The live typing smoke test exercised refresh and explicit
  disable, not a model-turn completion after the harness exited.
- Download handles in the live smoke test used file identifiers from the bot's
  acknowledged uploads. Real inbound media normalization is covered by fixtures;
  it has not yet been smoke-tested for every Telegram media type.
- No integrated VPS subscription/model/approval/UI acceptance run has been claimed.
  The runner is explicitly experimental. Pending approvals/input are preserved,
  but there is not yet an operator UI for resolving them.
- Desktop closed/unloaded wake-up, Discord, cron, transcription, album coalescing,
  durable replay, and a shared multi-client daemon remain outside v1 scope.

To extend live acceptance, use a dedicated configured chat and bot, send text and
media while the chat is active and idle, inspect the turn IDs, then enable typing
and let the owning turn finish or interrupt it. Never retry an unknown delivery
merely to make a smoke test pass. Check local outcomes without dumping bodies,
credentials, or Telegram download URLs.
