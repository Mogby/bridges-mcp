# Validation

## Automated coverage

Tests exercise persistent single-target focus, two connections routed to different
sessions, switching a connection without affecting another, idempotence,
concurrent focus calls, transfer serialized with in-flight delivery, persistence
failures, invalid/unauthenticated callers, and unloaded applications. Unfocused
messages are dropped without an implicit fallback.

Real authenticated HTTP MCP round trips verify that the bearer token selects the
application, caller metadata selects its session, explicit session arguments are
rejected, and focus requires a connection ID. Protocol instructions do not tell
agents to claim connections. In-memory SDK tests verify schemas and reject calls
without HTTP authentication context.

Unix WebSocket fixtures verify RPC multiplexing, disconnects, reconnects, and that
Bridges never calls thread/start or thread/resume. Process tests launch the actual
Bridges CLI without Codex or application sockets and exercise HTTP access,
shutdown, and restart. The optional BRIDGES_CODEX_TEST_BINARY test attaches to a
real independently launched App Server and confirms closing Bridges's connection
does not stop that process. These tests require local socket permissions.

Deterministic lifecycle tests cover typing refreshes, independent connections,
idempotence, in-flight cancellation, focus-transfer cleanup, stale-session typing
rejection, completion/replacement/disconnection, active steering, idle start,
known turn-boundary retries and ambiguous delivery without retry.

Telegram tests cover supported input normalization, separate trusted routing
metadata, duplicate updates, overlapping routes, webhook conflicts, payload
limits, partial/uncertain sends, downloads, and bounded expiring attachment handles.

## Independent-service live evidence

On 2026-11-14, the deployed independent HTTP service accepted a real model's
`focus` call using authenticated client identity and Codex host session metadata.
The assignment persisted on disk, and the model successfully sent a Telegram
reply and controlled typing for that connection.

Restarting each service independently preserved the focus map and application
session ID, left the other service's process unchanged, and restored authenticated
MCP access. This exposed and fixed a TCP TIME_WAIT restart issue; the process test
now stops the server with a keep-alive client still connected before rebinding.

A user-originated Telegram message reached the resumed session, which replied
successfully. Follow-up checks exposed stale historical `inProgress` entries in
Codex's turn listing after restart. Bridges now checks only the newest turn;
regression tests verify stale history cannot block current typing or substitute
for a completed newest turn. Live verification then confirmed typing enable/disable,
accepted active-turn steering, and completion observation. The final automated
suite at that point passed all 65 tests, including the installed Codex lifecycle check.

The application also successfully spawned a subagent, which ran `pwd` in its
configured workspace and completed. No experimental feature toggle was needed.
After restart, the application reported `gpt-6.1-sol`, high reasoning effort,
automatic approval review, and live web search from its own configuration.

## MarkdownV2 and reply metadata

The updated suite passes 71 tests. Coverage verifies MarkdownV2 is passed unchanged
with its parse mode for text, file captions, and photo captions, while preserving
destination/topic routing and returned message IDs. Incoming text, photo, voice,
audio, and document envelopes expose their message IDs and the immediate reply's
message ID when present, without replacing the body or exposing nested replies.

A live send through the deployed bridge returned Telegram entities for bold,
italic, inline links, and code. Jarvis was restarted to refresh MCP tool definitions.
A user's reply to that test reached the focused session with both its own message
ID and the expected original message ID. The model then successfully sent a reply
using the updated MarkdownV2 tool and enabled/disabled typing.

## Historical live evidence

Earlier live checks verified Telegram send/download tools, inline images, typing,
and subscription-backed model-originated MCP calls. The previous combined runner
also verified native web search, image generation, automatic approval review, and
replies following restart. Those results cover shared components; they do not by
themselves establish the independent-service focus lifecycle.

## Remaining boundaries

The integration uses the installed Codex App Server protocol. Other harnesses
need delivery adapters. Human approval/input requests need an operator interface
in the owning application. There is no durable inbox, transcription, album
coalescing, Discord/cron implementation, or distributed cross-host poller lock.
No model-generated instruction is used to auto-focus a connection. Application
instructions own that policy.

## Automatic inbound typing

Typing is started by accepted inbound delivery, without an agent MCP call. The
removed `telegram_set_typing` name is absent from discovery and protocol tools
and returns `unknown_tool` if a stale client invokes it. Lifecycle tests cover
initial sends, idempotent steering, completion/interruption, replacement, stale
acknowledgements, independent connections, focus transfer, disconnection,
shutdown, slow requests, and recovery from Telegram failure/rate limiting.
The HTTP and SDK discovery checks expect seven enabled tools.
