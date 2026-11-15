# Validation

Run `pytest -q`, `ruff check src tests`, and `ruff format --check src tests`.
Set `BRIDGES_CODEX_TEST_BINARY` to exercise an isolated installed Codex App Server.
Socket integration tests need permission to bind local Unix and loopback sockets.

Coverage includes shared automatic creation, first-message preservation without
initialization prompts, deleted-pointer replacement, persisted settings, uncertain
creation write-ahead markers, persistence failures, independent-session scheduling,
shared `/new_session`, both admin permission flags, strict session/backend references,
and six-tool MCP discovery with no focus API.

Existing lifecycle tests verify typing ownership, slow requests, completion and
replacement, stale acknowledgements, disconnections, active steering, idle turn
start, definite turn-boundary reconciliation and uncertain delivery without retry.
HTTP tests verify bearer-selected backend identity, host thread metadata, explicit
outbound destinations, invalid arguments and unauthorized callers. Telegram tests
cover entity-based command parsing, private/group menu registration, deduplication,
one inaccessible chat, routing, attachments, MarkdownV2, uncertain/partial sends,
webhook conflicts, poll offsets and duplicate updates.

The installed-server integration checks empty thread creation without a model turn,
loaded session access, model catalogs, and specific missing-thread classification.
Closing a Bridges client must leave the independently owned server process alive.

No Discord/cron adapters, durable inbox, transcription, distributed locking, or
interactive approval UI are included. Existing daemon approval policy is preserved.
