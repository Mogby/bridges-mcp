# Bridges MCP

Bridges connects Telegram conversations to shared logical sessions in an existing
Codex App Server daemon. It owns connection routing, durable Codex thread pointers,
session creation/resumption, and model/thinking settings. Codex owns execution,
conversation history, authentication, native tools, and sandbox/approval policy.
Bridges never launches or stops the daemon.

## Install and configure

Requires Python 3.11+ on Linux or macOS.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Copy [examples/app-server.yaml](examples/app-server.yaml) to a private location.
Give each daemon a `clients` entry containing its protected Unix WebSocket path
and a `token_env` naming a unique MCP bearer token of at least 32 characters.
Keep credentials in environment variables, never in YAML or command arguments.
The MCP endpoint binds only to `127.0.0.1`.

Define stable logical `sessions`. Each selects a client and absolute workspace
`cwd`, with optional `model` and `effort` defaults. Every Telegram connection
references one logical `session_id`. Multiple connections may reference the same
session. Their history and model/thinking settings are shared.

```yaml
clients:
  codex:
    token_env: CODEX_MCP_TOKEN
    socket_path: /var/lib/codex/app-server-control/app-server-control.sock
sessions:
  personal:
    client: codex
    cwd: /srv/assistant
telegram:
  bots:
    main:
      token_env: TELEGRAM_BOT_TOKEN
      admin: true
  connections:
    telegram_personal:
      bot: main
      chat_id: "123456789"
      session_id: personal
      allow_admin: true
```

A logical `session_id` is a Bridges configuration name, not Codex's native session
or thread identifier. Bridges persists one nullable `thread_id` per logical session
in `<state_dir>/<instance_id>/sessions.json`. There is no agent-controlled focus
or application-owned session pointer.

```sh
bridges discover-telegram --config /absolute/path/config.yaml --bot main
bridges validate --config /absolute/path/config.yaml
bridges serve --config /absolute/path/config.yaml
```

See [deployment and migration](docs/deployment.md), [design](DESIGN.md), and
[admin commands](docs/admin.md). One process polls each configured bot. Process
locks prevent duplicate listeners sharing a state directory. Bridges never removes
a webhook automatically. Group visibility depends on Telegram privacy settings.

## Session lifecycle

Startup creates no conversations and sends no initialization prompt. On incoming
messages, Bridges resolves the connection's logical session:

- Null pointer: create an empty Codex thread, save it, then deliver the message.
- Existing loaded thread: steer its active turn or start an idle turn.
- Existing unloaded thread: resume that same thread before delivering.
- Confirmed missing/deleted thread: create and save one replacement for the group.
- Temporary/unclassified failure: preserve the pointer; never replace history.

Each logical session has its own bounded queue and lock. All connections in a group
share creation, replacement, and delivery ordering; independent groups can progress
concurrently. There is no durable inbox or automatic retry of ambiguous deliveries.

Creation writes a durable pending marker before submitting `thread/start`. A lost
acknowledgement or interrupted final save cannot cause repeated automatic creation.
Check Codex state before explicitly requesting `/new_session` to recover an uncertain
creation. Corrupt state fails startup rather than silently losing pointers.

No synthetic initialization turn is sent. Codex loads workspace instructions in
its normal instruction-discovery process. The first actual message is the first
turn. Replies retain their originating connection; shared sessions do not broadcast.
Telegram is currently the implemented transport; Discord and cron need adapters.

## Admin commands

Both the bot's `admin` and the connection's `allow_admin` must be true. Both default
to false. Commands are handled by Bridges and never become model prompts.

- `/new_session`: create an empty thread and replace the shared pointer for every
  connection in the logical session. Old conversation history remains saved.
- `/model [model]`: list choices/current model or save a model for the shared session.
- `/thinking [level]`: list choices/current reasoning effort or save a supported level.

Settings persist across restarts and apply on the next idle turn. A fresh thread
in the same logical session retains its shared settings. Menu setup runs at startup
for admin-enabled bots and permitted chats, including private-chat menu buttons.

## MCP tools and messages

The server exposes `get_protocol_docs`, `telegram_send_text`, `telegram_send_images`,
`telegram_send_file`, `telegram_download_images`, and `telegram_download_file` when
Telegram connections exist. There is no `focus` or session-management MCP tool.
Authenticated bearer tokens select the daemon; trusted host metadata identifies the
calling thread. Outbound operations verify it is loaded in that daemon.

Incoming messages carry fenced YAML `bridge_context` metadata followed by unchanged
message text. Required fields are `bridge`, `connection_id`, `message_id`, and
`received_at`. Replies include `reply_to_message_id`. Attachments use bounded,
connection-scoped ephemeral handles. Audio is not transcribed. File paths are local
to the bridge host.

Ordinary assistant prose is not forwarded to Telegram. Use explicit send tools and
select the originating connection. Outgoing text/captions use MarkdownV2 unchanged;
escape literal punctuation according to Telegram's rules. Uncertain sends are not
retried. Partial image batches report completed IDs and the failed index.

Typing starts automatically after an incoming message is accepted. Refreshes stop
when that turn completes, changes, disconnects, or its shared session is replaced.
Slow typing requests do not block delivery. Approval/input requests are preserved;
Bridges does not answer them or weaken daemon policy.

Run `pytest -q`, `ruff check src tests`, and `ruff format --check src tests`.
Set `BRIDGES_CODEX_TEST_BINARY` for the optional installed Codex integration test.
