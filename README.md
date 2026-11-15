# Bridges MCP

Bridges is a shared MCP server that routes messaging connections into logical
agent sessions. Connections can share a session, conversation history, and settings.
Replies use explicit destinations and are never automatically broadcast.

Session routing, persistence, admin commands, and messaging tools are independent
of the agent harness. Harness adapters implement conversation creation/loading,
input delivery, model catalogs, and turn lifecycle. The bundled adapter supports
Codex App Server over a protected local Unix WebSocket. Other harnesses need an
adapter implementing the same interface.

## Install

Requires Python 3.11+ on Linux or macOS.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

See [configuration](docs/deployment.md), [the example](examples/app-server.yaml),
[admin commands](docs/admin.md), and [validation](docs/validation.md).

## Configure

Give each backend a `clients` entry with an adapter, socket path, and `token_env`
naming a unique MCP bearer token of at least 32 characters. Keep credentials in
environment variables, never YAML or command arguments. MCP binds to loopback.

Define logical `sessions`, each selecting a client, absolute workspace `cwd`, and
optional `model`/`effort` defaults. Every messaging connection references a stable
`session_id`. Multiple connections can reference the same logical session.

```yaml
clients:
  assistant:
    adapter: codex-app-server
    token_env: ASSISTANT_MCP_TOKEN
    socket_path: /run/assistant/harness.sock
sessions:
  personal:
    client: assistant
    cwd: /srv/assistant
telegram:
  bots:
    main:
      token_env: TELEGRAM_BOT_TOKEN
      admin: true
  connections:
    personal_telegram:
      bot: main
      chat_id: "123456789"
      session_id: personal
      allow_admin: true
```

```sh
bridges discover-telegram --config /absolute/path/config.yaml --bot main
bridges validate --config /absolute/path/config.yaml
bridges serve --config /absolute/path/config.yaml
```

One process polls each configured bot. Process locks prevent duplicate listeners
sharing a state directory. Bridges never removes webhooks automatically. Group
visibility depends on Telegram privacy settings and permissions.

## Shared sessions

A logical `session_id` is a stable Bridges name. Its backend conversation pointer,
`thread_id`, is stored once in `<state_dir>/sessions.json`. Downloads always use
`<state_dir>/downloads`. Each process uses its own `state_dir` and HTTP port; no
instance identifier or download-directory override is required.

On incoming messages:

- Null pointer: create a conversation, save the pointer, and deliver the message.
- Existing loaded conversation: deliver normally.
- Existing unloaded conversation: load that same conversation before delivery.
- Confirmed deleted/missing conversation: create one replacement for the group.
- Temporary or unclassified failure: preserve the pointer rather than replace history.

Creation sends no initialization prompt. The actual incoming message starts the
first turn. Each logical session has a bounded queue and lock; shared connections
cannot race creation/replacement, while independent sessions progress concurrently.
There is no durable inbox or automatic retry of ambiguous submissions.

A durable pending marker prevents repeated automatic creation after a lost
acknowledgement or interrupted final save. Inspect backend state before explicitly
using `/new_session` to recover uncertain creation. Corrupt state fails startup.

Telegram is the implemented messaging transport. Additional transports can route
through the same session manager; Discord and cron adapters are not included.

## Admin commands

Both bot `admin` and connection `allow_admin` must be true; both default to false.
Recognized commands are handled by Bridges and never become model prompts.

- `/new_session`: create an empty conversation and replace the shared pointer for
  every connection in the logical session. Previous history remains saved.
- `/model [model]`: show choices/current model or save a model for the shared session.
- `/thinking [level]`: show choices/current reasoning effort or save a supported level.

Settings persist and apply on the next idle turn, including new conversations in
the same logical session. Availability comes from the backend's model catalog.
At startup Bridges configures bot commands, permitted chat scopes, and private-chat
menu buttons.

## MCP tools and messages

With Telegram connections configured, the server exposes `get_protocol_docs`,
`telegram_send_text`, `telegram_send_images`, `telegram_send_file`,
`telegram_download_images`, and `telegram_download_file`. There is no agent-controlled
`focus` or session-management MCP tool.

Bearer authentication selects the backend. Host metadata identifies the calling
conversation; outbound tools verify it is loaded in that backend. Outbound
destinations are explicit and do not change session mappings.

Incoming messages carry fenced YAML `bridge_context` metadata and unchanged body
text. Required fields are `bridge`, `connection_id`, `message_id`, and `received_at`.
Replies include `reply_to_message_id`. Attachments use bounded, connection-scoped,
ephemeral handles. Audio is not transcribed. File paths are local to the bridge host.

Ordinary assistant prose is not forwarded to Telegram: use send tools and select
the originating connection. Outgoing text/captions use MarkdownV2 unchanged. Escape
literal punctuation according to Telegram's rules. Uncertain sends are not retried;
partial image batches report completed IDs and the failed index.

Typing starts automatically after accepted incoming delivery and stops when the
owning turn completes, changes, disconnects, or its shared session is replaced.
Slow typing requests do not block delivery. Bridges preserves backend approval/input
policy and never answers approval requests on the user's behalf.
