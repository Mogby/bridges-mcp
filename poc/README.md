# Desktop bridge proof of concept

This experiment checks two independent requirements: identifying the Codex chat
that calls an ordinary MCP tool, and delivering a later message to that same chat.
It uses local desktop IPC (steering), the installed Codex CLI (earlier queue
experiments), and the existing subscription login. There are no
OpenAI API keys, external services, or Telegram credentials.

## Current observations

Tested on 2026-11-14 with desktop-bundled Codex `0.169.0-alpha.11.1`.

| Check | Result |
| --- | --- |
| Chat identity at MCP startup | No thread ID in environment or initialize metadata. Shell-tool environment is different from MCP startup. |
| Public App Server attachment | No default App Server control socket exists in this desktop launch. The backend uses unnamed socketpairs. |
| Private desktop app-tools socket | Its path is supplied to shell tools, but a direct Python connection was closed. The app has native peer authorization. This is not a supported third-party transport. |
| Supported CLI submission | `codex queue --thread … --message …` accepted messages from both shell tools and the actual MCP server process. |
| Queued follow-up delivery | Confirmed: `bridges-poc-queue-01` arrived in the same chat after the submitting turn ended and triggered another agent turn. |
| Immediate active-turn delivery | Confirmed from the Python IPC helper: `bridges-steer-6ab3a9e9f036` arrived and was acknowledged inside the submitting turn, with the same turn ID. |
| Third-party MCP chat attribution | Confirmed: a real `inspect_context` tool call receives this chat's ID in `_meta.threadId`. No manually configured thread ID or desktop socket was supplied. |
| MCP-originated queued delivery (v0.1) | Confirmed: `send_test` discovered the chat ID, queued `bridges-mcp-ea72400357b0`, and the agent received that exact marker and bridge envelope in the same chat after the submitting turn finished. |
| MCP-originated steering (v0.2) | Confirmed end to end: `steer_test` delivered `bridges-mcp-3a1bfef99d6d` into the same active turn, where the agent acknowledged it. |
| Idle-chat wake-up | Confirmed: a delayed CLI submission arrived after the previous turn completed and automatically started a new turn in the same chat. |

The first test marker is `bridges-poc-queue-01`. It appeared after the submitting
turn finished, proving queued follow-up delivery, not immediate steering.

Idle-wake evidence for `bridges-idle-9029fb637ca6` (UTC on 2026-11-14):

- Previous turn completed at **15:51:45** (desktop thread record).
- Test submitted at **15:51:57.630865** (local sender log, CLI exit code 0).
- Receiving turn started at **15:51:58** (desktop thread record).

The chat had been idle for approximately 12.6 seconds before submission. The
message woke it without a user prompt or a `--resume` flag. This establishes
wake-up for an idle, loaded desktop chat; it does not establish behavior when
the app is closed or the thread is unloaded.

After the app restart, the real MCP process received no `CODEX_THREAD_ID`,
`CODEX_SESSION_ID`, or `CODEX_APP_TOOLS_PIPE_PATH` environment variables.
Its initialization also contained no metadata. The first tool call supplied
`threadId` alongside `callId`, `itemId`, `progressToken`, `sessionId`, `windowId`,
and `x-codex-turn-metadata`. The `threadId` exactly matched this chat.

At 15:54:29 UTC, `send_test` used that host-supplied ID to invoke the configured
Codex executable from the MCP process itself. The CLI returned exit code 0 and a
queued-message ID. This requires neither a desktop backend socket nor copied
subscription credentials.

The subsequent agent turn received `bridges-mcp-ea72400357b0` with the generated
`bridge_context` header. This completes the real MCP-to-Codex delivery test:
host-provided chat identity, submission from the MCP process, and agent receipt
all succeeded. The observed behavior is queued follow-up delivery, not insertion
into the already-active turn.

### Active-turn steering evidence

At **15:57:17 UTC**, `desktop_steer.py --steer-test` sent
`bridges-steer-6ab3a9e9f036` through the same-user desktop coordination socket
at `~/.codex/ipc/ipc.sock`. It discovered the chat's owning desktop client using
`thread-owner-discovery`, then sent `thread-follower-steer-turn` to that owner.
The owner forwards steering to its active App Server turn.

- Before sending, turn `01a1dac4-e6b2-7140-913f-ef2865ad7d5c` was `inProgress`.
- The successful IPC response returned that same turn ID.
- The desktop thread record contained the test as a user message within that
  same turn, still `inProgress` with no completion timestamp.
- The agent acknowledged the marker during that turn.

This establishes steering, rather than a queued follow-up. The client identifies
itself as `bridges-poc`; no app modification or authorization bypass was needed.
This is a different socket from the rejected private app-tools socket above.
The coordination protocol is internal and version-specific, not a supported
public integration API. App upgrades may break it.

### End-to-end MCP steering evidence

After refreshing the app, the actual MCP process (PID 77318) exposed
`steer_test`. At **16:31:48 UTC** on 2026-11-14 it delivered
`bridges-mcp-3a1bfef99d6d` after a two-second delay:

- `inspect_context` and `steer_test` both received this chat's identity through
  host-supplied `_meta.threadId`; no thread ID was manually configured.
- Before submission, turn `01a1dae6-57c7-7c71-a1f4-1d9eb9225cf7` was active.
- The MCP's `steer_result` log recorded success and returned that same turn ID.
- The desktop thread record showed the marker and the agent's acknowledgment
  inside that turn, still `inProgress` with `completedAt: null`.

This verifies the complete MCP-to-desktop steering path using the existing
subscription session. No queue fallback or additional test was scheduled.

### Standalone App Server: steering while idle

On 2026-11-14 at **16:34:24 UTC**, `app_server_idle_test.py` launched the installed
`codex app-server --stdio` with an isolated temporary `CODEX_HOME`. After
initialization, it created an ephemeral chat. `thread/start` reported
`status.type: idle` and zero turns. A direct `turn/steer` request returned:

```json
{"code": -32600, "message": "no active turn to steer"}
```

The generated protocol schema requires `expectedTurnId`; the test supplied a
UUID sentinel because an idle, never-started chat has no active turn ID.
Steering does not wake this idle chat. Use `turn/start` for idle delivery and
`turn/steer` with the active turn ID while busy.

This test needed no credentials or model request. It verifies idle validation
on standalone App Server, not subscription login, standalone active steering,
or idle behavior after a completed model turn. The temporary server was stopped
and its temporary home removed. Reproduce with:

```sh
python3 poc/app_server_idle_test.py --codex /absolute/path/to/codex
```

## Probe server

`mcp_probe.py` is a small stdio MCP server with no Python package dependencies.
It implements the 2025-11-25 protocol subset needed for this experiment, including
initialization, tool discovery, and tool calls. It is not a production MCP SDK.

- `inspect_context` reads only host-supplied tool-call metadata. It reports the
  thread ID, where it came from, metadata key names, and whether relevant
  environment variables are present. It never guesses from a working directory
  or the most recently active chat.
- `steer_test` captures that call's thread ID and schedules one labeled message
  through desktop IPC. It returns a unique marker before sending. It refuses to
  send if the host supplies no chat identity. `delay_seconds` ranges from 0 to 30.
  It requires an active turn and never falls back to queueing or retries an
  uncertain submission. Failures are recorded in the log.

The server checks direct thread metadata, nested `thread.id`, and
`x-codex-turn-metadata`. The latter matches the parsing used by the bundled
desktop app-tools MCP. Direct `_meta.threadId` attribution is now verified on
this installed desktop version; the other formats remain compatibility fallbacks.

Logs are written to ignored `poc/.runtime/mcp-<pid>.jsonl` files. They include
selected environment paths/IDs, metadata key names, and test submission results.
They do not dump arbitrary environment variables, credentials, or conversation
history. No messages are sent at startup.

## Run the experiment

The server is registered and loaded on this machine as `bridges_poc` v0.2,
exposing `inspect_context` and `steer_test`.
To reproduce
on a fresh installation, refresh MCP connections or reopen the desktop app after
registration, return to the same chat, and call `bridges_poc.inspect_context`.

1. Check that `thread_id` matches this chat and `source` names request metadata.
2. When explicitly running another test, call `steer_test` with a short delay
   while the agent continues working. Observe the marker within the active turn.
3. Check the matching `steer_result` or `steer_error` log. Compare the returned
   turn ID with the desktop thread record; only observing the marker in that
   active turn proves steering. A scheduled tool result does not prove delivery.

No further test is scheduled automatically. The historical idle-wake script
uses `codex queue`; the current probe intentionally tests steering only.

Tests operate solely on the chat that invokes the tool. They do not create,
rename, archive, or modify other chats. Delayed jobs are in memory and can be
lost if the MCP process exits, consistent with the intended minimal bridge.

For another machine, register with its absolute Python and project paths:

```sh
codex mcp add bridges_poc -- /absolute/path/to/python3 \
  /absolute/path/to/bridges-mcp/poc/mcp_probe.py
```

Remove the test registration when finished:

```sh
codex mcp remove bridges_poc
```

The read-only `desktop_probe.py` records the unsuccessful private-socket
experiment. It is retained for reproducibility, not as the delivery mechanism.

## Checks

```sh
python3 -m unittest discover -s poc -p 'test_*.py' -v
```

Five tests cover metadata parsing, refusal to send without identity, destination
preservation, no retry/queue fallback on steering failure, and a stdio
handshake/tool-call round trip. Test sends are mocked;
these checks do not establish desktop delivery.

## Integration implications

On this tested desktop version, the bridge can bind when the agent first calls
`get_protocol_docs()` or a dedicated setup tool. It does not need a user-entered
chat UUID. Binding only becomes available after that call; MCP startup by itself
does not identify a target chat. A production bridge must retain an explicit
binding and avoid silently replacing it when another chat discovers its tools.

Both active-turn steering and idle wake-up of this loaded desktop chat are now
verified separately. A desktop integration can use steering when busy and an
idle submission when idle. A production implementation still needs to handle
the race where the active turn ends during delivery; it must not blindly queue
after an ambiguous steering timeout because that could duplicate a message.
The current minimal probe reports failure instead.

For a persistent VPS session, a bridge-owned App Server can use its explicit
`turn/steer` and `turn/start` interfaces. The desktop route demonstrated here
uses internal IPC, so these transports should sit behind a small implementation
boundary inside the same bridge process, not require a separately managed
service. End-to-end steering from the refreshed MCP process is now verified.

Sources:

- [Codex queue documentation](https://learn.chatgpt.com/docs/developer-commands#codex-queue)
- [App Server protocol](https://learn.chatgpt.com/docs/app-server)
- [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=app)
- Bundled `plugins/openai-bundled/plugins/codex-app-tools/server.mjs`: request
  metadata parsing and private socket framing, inspected read-only.
- Installed desktop JavaScript bundles: coordination socket framing, owner
  discovery, and follower steering, inspected read-only.

OpenAI also documents [MCP Events](https://developers.openai.com/plugins/build/mcp-events),
but its stated availability is cloud Work chats and dots, not this local Codex
chat. It is not assumed to solve this experiment.
