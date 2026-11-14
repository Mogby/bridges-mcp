# Bridges MCP

Bridges connects Telegram conversations to one Codex chat using your Codex
subscription. One Python application owns incoming long polling, Codex delivery,
and MCP tools for sending messages, downloading attachments, and maintaining typing.
No OpenAI API key is used.

Incoming messages steer the owned App Server chat while it is busy and start a
turn when it is idle. Ordinary assistant replies are **not** forwarded to Telegram:
the agent must use a Bridges send tool.

Bridges runs as a standalone application on your machine or VPS. It launches its
own Codex App Server process and creates or resumes one persistent chat. It does
not attach to a chat in the Codex desktop app or run as a desktop stdio MCP server.

## Status

App Server is the only supported transport. The runner is still **experimental**:
a live VPS deployment has verified subscription-backed model execution, MCP
callbacks, Telegram replies, native web search and image generation, and automatic
approval review. Human approval/input requests are preserved without an operator
UI for resolving them. Existing Codex approval and sandbox policy remains in force.

Automated tests cover startup, authenticated loopback MCP access, restart/resume,
child shutdown, delivery decisions, and typing lifecycle. See
[validation evidence and remaining checks](docs/validation.md) and the
[design](DESIGN.md).

## Install

Requires Python 3.11+ on macOS or Linux, an installed Codex CLI executable, and a
subscription login on the same host. Windows is not supported in this release.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The installed entry point is `.venv/bin/bridges`. For a regular installation,
`pip install .` also works.

For an always-on VPS with a separate agent workspace, see the
[systemd deployment guide](deploy/systemd/README.md).

## Create and configure a Telegram bot

1. Create a bot with Telegram's [@BotFather](https://t.me/BotFather) using `/newbot`.
2. Supply its token through an environment variable named in your configuration,
   such as `TELEGRAM_BOT_TOKEN`. Never put tokens in YAML or commit them.
3. Copy [examples/app-server.yaml](examples/app-server.yaml) to a private local file.
4. Open a private conversation with the bot and send `/start`. For a group, invite
   the bot and send it a message it can see.
5. Discover the chat ID, then replace the example `chat_id` with the returned string:

```sh
.venv/bin/bridges discover-telegram --config /absolute/path/bridges.yaml --bot main
.venv/bin/bridges validate --config /absolute/path/bridges.yaml
```

Discovery reads pending messages without advancing the update offset and prints
only conversation identifiers and labels, never their bodies. Stop other pollers
before running it. If no conversation appears, send a new message and try again.
A configured webhook causes a setup error; Bridges never deletes it for you.

Group visibility depends on Telegram's privacy mode and the bot's permissions.
Configure these through BotFather and the group settings as appropriate. Allowing
everyone to instruct Codex does not make messages hidden from the bot visible.
See the [Telegram bot FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

A minimal configuration:

```yaml
instance_id: personal
codex:
  executable: /absolute/path/to/codex
  cwd: /absolute/path/to/workspace
telegram:
  bots:
    main:
      token_env: TELEGRAM_BOT_TOKEN
  connections:
    personal:
      bot: main
      chat_id: "123456789"
      label: My private chat
```

For several conversations, add connections under the same bot. Each bot gets
exactly one poller. Optional `message_thread_id` routes one group topic. An
all-topics route and a topic-specific route for the same bot/chat overlap and
are rejected. Chat IDs must be quoted YAML strings. An absent, empty mapping,
or empty list of connections disables Telegram tools and listeners.

Optional settings: `state_dir` (default `~/.local/state/bridges`), `download_dir`
(default `<state_dir>/<instance_id>/downloads`), `buffer_size` (256),
`attachment_limit` (4096), and `attachment_ttl_seconds` (86400).
Configuration changes and token rotations require a process restart.

## Run one persistent Codex session

Log in on the bridge host with the subscription flow, then start Bridges with its
token environment variable available:

```sh
codex login
.venv/bin/bridges serve --config /absolute/path/bridges.yaml
```

One application owns the entire lifecycle:

1. Bind an authenticated, loopback-only MCP endpoint on `127.0.0.1`.
2. Launch a `codex app-server --stdio` child using your existing subscription login.
3. Create a chat on first start, or resume the saved chat after restart.
4. Validate its live state, then start the configured Telegram pollers.

Send a Telegram message to the configured bot to start work. Tell the agent where
to send replies; for example, “Reply to my messages through Telegram.” The agent
reads `get_protocol_docs` when it first receives bridge input. That tool reports
the application-owned binding and cannot change the destination chat.

The child receives a process-local MCP URL and bearer token through configuration
overrides. It connects to the existing endpoint instead of spawning another
bridge. No global Codex config is rewritten and no separate `codex mcp add`
registration is needed. `codex.port` defaults to `0` to choose an available
loopback port; `codex.mode`, if supplied, accepts only `app_server`.

The runner verifies a ChatGPT account and removes OpenAI API-key environment
fallbacks from the child. It does not copy credentials or override Codex
approval/sandbox settings. Use the same host/filesystem for the bridge and Codex.

Stop the application with Ctrl-C or SIGTERM; its listeners, endpoint, and child
process shut down together. Your process supervisor can run this same command
and restart it after failure. A second process using the same instance or bot
fails its lock rather than starting another poller. All instances sharing bots
on a host must use the same `state_dir`; locks are not shared across hosts.

To start a fresh chat, stop Bridges and clear its binding:

```sh
.venv/bin/bridges reset-binding --config /absolute/path/bridges.yaml
```

The next `serve` creates a new App Server chat. Reset does not disconnect a running
process or delete the old chat. A docs/tool call cannot select an existing chat.

### Migrating from the former desktop mode

Stop the old bridge and remove its desktop MCP registration. Set `codex.mode` to
`app_server` (or omit it), remove `codex.ipc_path`, and set the workspace `codex.cwd`.
Old desktop configs are rejected. If reusing its `instance_id` and `state_dir`,
run `reset-binding` with the updated config before starting the application; old
desktop chat bindings are deliberately not resumed.

## Message format and tools

Only text, images, audio/voice, and documents enter the Codex chat. Edits,
reactions, deletions, service messages, and connection status are ignored.
Metadata is fenced to prevent Markdown from interpreting it as headings:

````text
```yaml
bridge_context:
  bridge: telegram
  connection_id: personal
  received_at: '2026-11-14T17:24:30Z'
```

Hello from Telegram!
````

Attachment messages add `attachments: [{type: image|audio|file, id: a_...}]` to
the header. The original message or caption follows the closing fence unchanged;
further headers or code fences in the body are just body text. Telegram file IDs
stay server-side in bounded, expiring handles.

| Tool | Purpose |
| --- | --- |
| `get_protocol_docs` | Owned chat binding, enabled protocols, connections, and reply rules |
| `telegram_send_text` | Plain text, up to 4096 characters |
| `telegram_send_images` | Ordered local images; caption on first, up to 1024 characters |
| `telegram_send_file` | One local file with optional caption |
| `telegram_download_images` | Image handles to paths, dimensions, and bounded inline image content |
| `telegram_download_file` | File/audio handle to local path and metadata; no transcription |
| `telegram_set_typing` | Enable ongoing typing for the calling turn, or stop it |

Every Telegram tool accepts `connection_id`. Omission is allowed only when
exactly one connection is configured. There is no last-active destination or
automatic reply policy. The user instructs the agent where to send results.

Downloads are limited to 20 MB. Photo uploads are limited to 10 MB and Telegram's
dimension limits; document uploads to 50 MB. The bridge and Codex must share a
filesystem. Downloaded files use generated filenames; original names are metadata.
Files remain in the download directory until you delete them; handle expiry does
not delete files. Large image downloads return paths even when the bounded MCP
inline-content budget is exhausted.

Typing refreshes approximately every four seconds until explicitly disabled or
the owning turn completes, fails, is interrupted, loses lifecycle connectivity,
or the bridge shuts down. Sending a message does not stop it. Telegram can show
the last typing action for up to five more seconds after cancellation.

Errors are MCP `isError: true` results with a machine-readable `code`. A send
with `delivery_unknown` must not be blindly retried. Partial image batches report
`completed_message_ids`, the zero-based `failed_index`, and whether the failed
send's outcome is unknown. Payloads are validated before the first upload.

## Delivery limits and operations

This release deliberately has no durable inbox or exactly-once guarantee.
Offsets, duplicate suppression, attachment handles, and inbound buffers are in
memory. Overflow and failed deliveries are logged locally and dropped. Restart
may replay pending Telegram updates. Delivery decisions are serialized only until
submission acknowledgement, never until the model turn finishes.

Delivery uses `turn/steer` with the observed active turn ID or `turn/start` while
idle. A definite turn-boundary rejection allows one reconciled retry; a timeout
or lost response is an unknown outcome and is not retried. Turn notifications
and status snapshots stop typing when ownership cannot be verified.

Pending approval/input requests remain active states. Incoming messages do not
approve an operation or answer a pending request. There is no operator UI for
these requests yet; do not treat this runner as an unattended approval mechanism.

Logs contain operational codes and outcomes, not message bodies, bot tokens,
authenticated download URLs, or raw upstream exceptions. Runtime state and secrets
must stay out of Git. Run these checks before changing the transport:

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

The production server uses the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
Telegram behavior follows the [Bot API](https://core.telegram.org/bots/api).
MIT licensed; see [LICENSE](LICENSE).
