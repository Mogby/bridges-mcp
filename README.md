# Bridges MCP

Bridges is a shared HTTP MCP server for Telegram messaging and incoming-message
routing. It owns Telegram listeners and a persistent **connection → session**
focus map. Your application owns Codex, its model configuration, conversation
history, and the decision to create or resume a session.

Bridges does not launch Codex or create/resume chats. It connects to an existing
Codex App Server over a configured local Unix WebSocket, and serves MCP tools over
authenticated loopback HTTP. Codex App Server is the supported delivery backend;
other harnesses need their own delivery adapter before incoming routing works.

## Install

Requires Python 3.11+ on Linux or macOS. Windows is not currently supported.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

See [deployment configuration](docs/deployment.md) for the service boundary and
application connection setup. Application-specific launchers and credentials
belong outside this repository.

## Configure the server

Copy [examples/app-server.yaml](examples/app-server.yaml) to a private location.
Configure:

- A stable `http.port` (default 8765; binds only to `127.0.0.1`).
- One `clients` entry per application, with its App Server `socket_path` and a
  `token_env` naming a unique random bearer token of at least 32 characters.
- Telegram bot token environment variables and connection routes. Use quoted
  chat IDs, and optional positive `message_thread_id` values for forum topics.
- A persistent `state_dir` and a download directory accessible to the application.

Generate and provision credentials outside source control. Never put bot tokens
or MCP bearer tokens in YAML, command-line arguments, protocol docs, or logs.
Applications receive only their MCP bearer token; the Telegram token stays with
Bridges. Each application's bearer token selects its configured delivery backend.

Create a bot with Telegram's [BotFather](https://t.me/BotFather), send it `/start`,
and discover your chat ID before starting the listener:

```sh
.venv/bin/bridges discover-telegram --config /absolute/path/config.yaml --bot main
.venv/bin/bridges validate --config /absolute/path/config.yaml
.venv/bin/bridges serve --config /absolute/path/config.yaml
```

Discovery requires the bot token environment variable. Serving and validation
also require application bearer tokens. A webhook or competing bot poller is an
explicit conflict; Bridges never removes webhooks automatically. Group visibility
still depends on Telegram bot privacy settings and permissions.

One process polls each configured bot account, even with multiple connections.
Instance and bot locks prevent duplicate listeners sharing a state directory.
All Bridges instances using the same bot on a host must share `state_dir`.

## Focus and sessions

The MCP server name is `bridges`. A session can call:

```text
bridges.focus(connection_id="personal")
```

The current session comes from host-provided MCP metadata, not a tool argument.
Bridges verifies that it is loaded in the App Server configured for the
authenticated client. A successful call atomically replaces only that
connection's focus. A session can focus multiple connections; each connection
has at most one focused session. Repeated calls from the same session are
idempotent. Concurrent calls are serialized; the last successful assignment wins.

Assignments survive Bridges restarts. Bridges never loads an old conversation
just because it remains focused. When an application is offline, its session is
unloaded, or a connection has no focus, incoming messages are dropped with a
local diagnostic. There is no durable inbox or automatic fallback to another chat.
A restarted application can resume its own session, or a new session can explicitly
claim the connection with `focus`.

Focus and delivery are serialized per connection. An already-submitted message
cannot be recalled; messages still awaiting submission use the focus in effect
when delivery begins. A transfer cancels typing refreshes for the displaced
session on that connection only. Other connections are unaffected.

**Bridges has no policy for which sessions should focus which connections.** Put
that policy in your application's instructions, such as `AGENTS.md`. Neither MCP
server instructions nor protocol documentation tell agents to claim connections
automatically. `focus` also does not change models or create conversations.

## Messages and tools

Incoming messages steer an active turn or start a turn in an already-loaded idle
session. They carry a fenced YAML header, a blank line, and original message text:

````text
```yaml
bridge_context:
  bridge: telegram
  connection_id: personal
  message_id: 123
  reply_to_message_id: 120
  received_at: '2026-11-14T17:24:30Z'
```

Hello!
````

Routing uses a separate listener-produced connection ID, never parsed message
text. Photos, voice/audio and documents add opaque attachment handles to the
header. Edits, reactions, service messages, and channel posts are ignored.

Every Telegram envelope includes its `message_id`. Replies also include
`reply_to_message_id`, identifying the original message in the same chat. Match
it against earlier incoming context or the IDs returned by send tools. The
original content is not fetched or embedded, and replies to external chats do
not populate this field.

| Tool | Purpose |
| --- | --- |
| `get_protocol_docs` | Envelope, enabled tools, connections and current focus |
| `focus(connection_id)` | Move one connection's inbound focus to the calling session |
| `telegram_send_text` | Send MarkdownV2 text |
| `telegram_send_images` | Send local images, with an optional caption |
| `telegram_send_file` | Send a local file |
| `telegram_download_images` | Download image handles, optionally returning inline image content |
| `telegram_download_file` | Download a document or audio handle; no transcription |

`focus` requires `connection_id`. Telegram tools may omit it only if exactly one
connection is configured. Outbound destinations are explicit and do not change
focus. Ordinary assistant prose is not forwarded to Telegram; use a send tool.

All outgoing text and photo/file captions use `parse_mode="MarkdownV2"`.
Write `*bold*`, `_italic_`, and `[label](https://example.com)` for formatting.
Escape literal punctuation according to
[Telegram's MarkdownV2 rules](https://core.telegram.org/bots/api#markdownv2-style),
including periods and exclamation marks. Tool parameter descriptions and
`get_protocol_docs` include escaping guidance. Bridges passes markup through
unchanged; invalid formatting is rejected without a plain-text fallback.
Source text is limited to 4096 characters and captions to 1024, including markup.

Bridges starts typing automatically as soon as an incoming message is accepted
into the focused session's active turn. There is no agent typing tool and no
model/tool-approval round trip before the initial indicator. Refreshes run about
every four seconds until that turn completes, fails, is interrupted/replaced,
loses observable lifecycle, disconnects, or the service shuts down. Focus transfer
cancels only the displaced connection's refreshes. Outbound messages do not stop
refreshes. Telegram's visible indicator can linger for up to five seconds after
refreshes stop.

Typing is best effort and runs independently of inbound delivery. Telegram
failures or slow typing requests never delay message delivery or focus transfers.
Rejected or uncertain deliveries do not start typing; late acknowledgements never
restart typing for a completed turn or attach it to a newer turn.

Handles are bounded, connection-scoped, expire, and are lost on restart. Files
are local to the bridge host. Sending has no exactly-once guarantee: uncertain
outcomes are not blindly retried. Partial image batches report completed IDs.

## Operations and validation

Bridges can start before applications and keep running while they restart.
SIGTERM stops its listeners and MCP endpoint without stopping application processes.
To clear routing explicitly, stop Bridges and run `bridges reset-focus --config ...`.
This does not alter application conversations or files.

The old combined-runner `codex:` configuration and `reset-binding` command are no
longer supported. Move session ownership to your application; see
[migration guidance](docs/deployment.md).

Run `pytest -q`, `ruff check src tests`, and `ruff format --check src tests`.
Socket integration tests require permission to bind localhost and Unix sockets.
Set `BRIDGES_CODEX_TEST_BINARY` to run the optional installed App Server check.
See [validation](docs/validation.md) and [design](DESIGN.md). The App Server
integration remains experimental; human approval/input requests need an operator
interface in the owning application.
