# Validation

Run `pytest -q`, `ruff check src tests`, and `ruff format --check src tests`.
Set `BRIDGES_CODEX_TEST_BINARY` to exercise an isolated installed Codex App Server.
Socket integration tests need permission to bind local Unix and loopback sockets.

Coverage includes shared automatic creation, first-message preservation without
initialization prompts, deleted-pointer replacement, persisted settings, uncertain
creation write-ahead markers, persistence failures, independent-session scheduling,
shared `/new_session`, both admin permission flags, strict session/backend references,
and MCP discovery with Telegram and scheduler tools but no focus API.

Existing lifecycle tests verify typing ownership, slow requests, completion and
replacement, stale acknowledgements, disconnections, active steering, idle turn
start, definite turn-boundary reconciliation and uncertain delivery without retry.
HTTP tests verify bearer-selected backend identity, host thread metadata, explicit
outbound destinations, invalid arguments and unauthorized callers. Telegram tests
cover entity-based command parsing, private/group menu registration, deduplication,
one inaccessible chat, routing, attachments, MarkdownV2, uncertain/partial sends,
webhook conflicts, poll offsets and duplicate updates.

Reaction and quote tests cover additions, replacements, removals, anonymous counts,
actor identity, malformed updates, reaction polling subscriptions and deduplication,
forum-topic isolation and expired mappings, outgoing emoji/custom/clear requests,
caller authorization, uncertain acknowledgement handling, and UTF-16 quote metadata
without changing the new message body. Mocked delivery does not prove that Telegram
will supply private-chat reaction updates.

Group bot-message tests cover sender metadata, sender_chat precedence, a finite
allowance after human input, human-only resets, expiry, bounded guard state,
own-bot filtering, bot-authored admin command bypass, and normal group delivery.

The installed-server integration checks empty thread creation without a model turn,
loaded session access, model catalogs, and specific missing-thread classification.
Closing a Bridges client must leave the independently owned server process alive.

Context tests cover last-request versus cumulative token counts, unavailable limits,
thread/backend isolation, compaction start/completion and replay, restart persistence,
disconnect freshness, storage failures, MarkdownV2 preservation, long messages, and
`/context` authorization without model work.

No Discord adapter, durable inbox, transcription, distributed locking, or
interactive approval UI are included. Existing harness approval policy is preserved.
