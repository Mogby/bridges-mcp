# Validation record

Implementation and local validation: 2026-11-14. App Server is the only supported
Codex transport. Installed Codex version checked: `0.169.0-alpha.11.1`. Its generated
JSON schema was inspected for initialization, thread creation/read/resume/turn
pagination, start/steer, and lifecycle notification fields. Generated schemas are
not shipped here.

## Automated checks

58 tests passed, including the installed-App-Server check and localhost process
integration tests. Ruff lint and formatting checks also passed.

The pytest suite exercises configuration/routing validation, token redaction,
instance and bot locks, persistent App Server binding, metadata conflicts,
disabled tool visibility, every inbound media mapping, ignored update kinds,
handle expiry and connection scope, bounded bursts, one poller per bot, offsets
and duplicates, webhook conflict, payload limits, partial sends, ambiguous sends
without retry, download metadata and paths, and MCP initialization/discovery/tools.

App Server is the default and only accepted mode. Removed transport settings and
old bindings are rejected. Startup creates or resumes the application's chat,
verifies live state, and persists its binding before polling starts. An MCP call
cannot claim or redirect the session, even while startup is in progress. Tests
cover these conditions and startup failures without starting listeners.

A fake clock exercises typing across refresh intervals, idempotent enable/disable,
two connections, cancellation during an in-flight action, turn replacement,
completion/interruption, disconnect, and outbound sends. Transport fixtures cover
active steering, idle start, uncertain outcomes without retry, a definite
turn-boundary rejection with one bounded retry, stale completion events, and
approval/input states remaining active without being answered.

MCP tests use the official SDK in memory and the authenticated HTTP endpoint.
Process integration tests launch the actual CLI with a fake Codex executable and
real localhost HTTP connections. They verify:

- The endpoint is reachable and requires a bearer token before the child initializes.
- API-key environment variables are removed from the child.
- One application creates the first chat and resumes that same binding on restart.
- MCP calls from the owned chat succeed; calls from another chat are rejected.
- SIGTERM shuts down the application and its child without leaving an orphan.
- Missing subscription login produces a concise error, creates no binding, and
  releases the child process.

The fake executable is a test fixture, not a bridge type or an installed server.
It makes no model request and uses no real subscription credentials or Telegram
connection. These process tests require permission to open localhost sockets.

The optional `BRIDGES_CODEX_TEST_BINARY` test launches the real installed App Server
with an isolated, unauthenticated `CODEX_HOME`. It verifies that subscription login
is required and that the child shuts down, without making a model request.

## Telegram API smoke-test evidence

The user supplied a test bot token locally and sent a message to the bot. Earlier
live checks verified the Telegram components shared by the current implementation:
all three send tools, both download tools, inline MCP image content, file-byte
integrity, and three acknowledged typing actions across two refresh intervals.
Outbound sends did not stop typing. The harness explicitly disabled typing and
closed its listeners and clients. The user then rotated the token.

Those checks used the former desktop transport. That transport and its tests
have been removed, and its steering results do not establish App Server delivery.
Tokens, private chat identifiers, test artifacts, and downloaded files remain
excluded from version control. The current envelope uses a fenced YAML metadata
header, with no `---` separator, as requested after the initial smoke test.

## Live App Server VPS acceptance

On 2026-11-14, a subscription-backed Ubuntu 24.04 deployment using Codex 0.169.0
received a real Telegram message, started the bound session, and successfully
called both protocol documentation and Telegram send tools with host-attributed
MCP metadata. The Telegram API acknowledged the model-originated reply.

Separate model-backed smoke tests using the service CODEX_HOME and workspace
verified shell writes, native web search, and native image generation with a PNG
saved to disk. An explicitly escalated read-only command completed with automatic
approval review. App Server config and thread responses independently confirmed
the working directory, reviewer, approval policy, and workspace-write sandbox.
Systemd restart shut down cleanly and resumed the same bound thread. Three
subsequent inbound messages each produced a successful model-originated Telegram
send through MCP, verifying that the renewed endpoint works after resume.
See the [deployment guide](../deploy/systemd/README.md) for configuration and paths.

## Remaining acceptance boundaries

- Live Telegram steering during an active model turn still needs an explicit
  integrated acceptance check. Idle wake-up, model execution, MCP callbacks,
  and replies after restart have passed the live deployment checks above.
- Pending approvals/input are preserved, but there is no operator UI for resolving
  them. Incoming messages must never answer or approve these requests implicitly.
- Completion/failure/interruption typing cancellation is covered by deterministic
  lifecycle tests. The live Telegram test exercised refresh and explicit disable.
- Live attachment download handles came from the bot's acknowledged uploads.
  Real inbound media normalization is covered by fixtures; every Telegram media
  type has not yet been tested live.
- Discord, cron, transcription, album coalescing, durable replay, and a shared
  multi-client daemon remain outside v1 scope.

For live acceptance, run one application with a dedicated configured chat and bot,
send text/media while its owned App Server chat is active and idle, inspect the
turn IDs, then enable typing and finish or interrupt its owning turn. Never retry
an unknown delivery just to make a test pass. Inspect operational outcomes without
dumping bodies, credentials, or Telegram download URLs.
