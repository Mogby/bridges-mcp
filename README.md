# Bridges MCP

Bridges connects Telegram conversations to one Codex chat using your Codex
subscription. One Python application owns incoming long polling, Codex delivery,
and MCP tools for sending messages, downloading attachments, and maintaining typing.
No OpenAI API key is used.

Incoming messages steer a busy chat. Loaded idle desktop chats use `codex queue`;
the standalone App Server transport uses `turn/start`. Ordinary assistant replies
are **not** forwarded to Telegram: the agent must use a Bridges send tool.

## Status

The desktop path has been tested on macOS with desktop-bundled Codex
`0.169.0-alpha.11.1`. Live checks covered real Telegram input steering the active
chat, all send/download tools, inline image content, typing refreshes, and the
desktop lifecycle subscription. Desktop IPC is private and version-specific.

The VPS App Server runner is implemented but **experimental**: subscription-backed
restart/resume, model execution, MCP callbacks, and interactive approval/input
handling still need an integrated VPS acceptance run. It preserves pending
approval/input requests without answering them. It currently has no operator UI
for resolving those requests; it must not be used to bypass your Codex policy.

See [validation evidence and remaining checks](docs/validation.md), the
[design](DESIGN.md), and the older [proof of concept](poc/README.md).

## Install

Requires Python 3.11+ on macOS or Linux, an installed Codex executable, and a
subscription login on the same host. Windows is not supported in this release.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The installed entry point is `.venv/bin/bridges`. Use absolute paths in MCP
registration. For a regular installation, `pip install .` also works.

## Create and configure a Telegram bot

1. Create a bot with Telegram's [@BotFather](https://t.me/BotFather) using `/newbot`.
2. Supply its token through an environment variable named in your configuration,
   such as `TELEGRAM_BOT_TOKEN`. Never put tokens in YAML or commit them.
3. Copy [examples/desktop.yaml](examples/desktop.yaml) to a private local file.
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
  mode: desktop
  executable: /absolute/path/to/codex
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

## Connect a desktop chat

Ensure the desktop app's MCP process inherits the token environment variable.
Launching a GUI app from Finder may not inherit your terminal's environment; a
local launcher can load your secret from a protected file into that variable
before executing `bridges`. Keep the launcher and secret outside version control.

Register the server as `bridges`:

```sh
codex mcp add bridges -- /absolute/path/bridges-mcp/.venv/bin/bridges \
  serve --config /absolute/path/bridges.yaml
```

Refresh MCP connections or restart the desktop app. In the **one selected chat**,
ask the agent to call `bridges.get_protocol_docs()` and discover its tools. This
first call binds the instance using the host's `_meta.threadId`. Polling starts
only after the desktop owner is reachable and its state can be observed.

The same binding is reused after restart only after reachability validation.
Other chats cannot silently take over or use send/download/typing tools. Another
process using the same instance or bot fails its lock rather than starting a
second poller. All instances sharing bots must use the same `state_dir`.

To intentionally select a different chat, stop the MCP process, then run:

```sh
.venv/bin/bridges reset-binding --config /absolute/path/bridges.yaml
```

Bootstrap the new chat after restarting the server. Do not register a separate
listener service. Desktop polling ends when the MCP connection closes.

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
| `get_protocol_docs` | Bootstrap, enabled protocols, connections, and reply rules |
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

## VPS / standalone App Server (experimental)

Use [examples/app-server.yaml](examples/app-server.yaml). Log in on the VPS with
`codex login` using the subscription flow, then run one process:

```sh
.venv/bin/bridges serve --config /absolute/path/app-server.yaml
```

Bridges binds an authenticated, loopback-only MCP endpoint before launching its
own `codex app-server --stdio` child. The child receives a process-local MCP URL
and bearer token through configuration overrides. It connects to that endpoint
instead of spawning another bridge. No global Codex config is rewritten.

The runner verifies a ChatGPT account and removes OpenAI API-key environment
fallbacks from the child. Existing Codex approval/sandbox settings remain in
force. The chat ID is persisted and resumed on restart; messages are not persisted.
A supervisor may restart the single application after a child or endpoint failure.
Pending approvals and input remain active states and are not answered by incoming
messages. See the status limitations above before deploying this mode.

## Delivery limits and operations

This release deliberately has no durable inbox or exactly-once guarantee.
Offsets, duplicate suppression, attachment handles, and inbound buffers are in
memory. Overflow and failed deliveries are logged locally and dropped. Restart
may replay pending Telegram updates. Delivery decisions are serialized only until
submission acknowledgement, never until the model turn finishes.

Desktop observation uses the same-user coordination socket, normally
`~/.codex/ipc/ipc.sock`, with an owner snapshot followed by revisioned patches.
A protocol mismatch or patch gap stops typing and requires a fresh snapshot.
No private app-tools socket or authorization bypass is used.

Desktop idle delivery has a narrow race: a turn can start between the state check
and CLI queue submission, so that message may wait for the next turn. Queue is
never a fallback after an ambiguous steering timeout. App-closed and unloaded-chat
wake-up are unsupported. The App Server transport uses explicit lifecycle methods.

Logs contain operational codes and outcomes, not message bodies, bot tokens,
authenticated download URLs, or raw upstream exceptions. Runtime state and secrets
must stay out of Git. Run these checks before changing the transport:

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
python3 -m unittest discover -s poc -p 'test_*.py' -v
```

The production server uses the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
Telegram behavior follows the [Bot API](https://core.telegram.org/bots/api).
MIT licensed; see [LICENSE](LICENSE).
