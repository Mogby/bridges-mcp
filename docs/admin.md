# Telegram application control

Admin commands are optional and require **both** permissions:

```yaml
clients:
  assistant:
    token_env: ASSISTANT_MCP_TOKEN
    socket_path: /run/assistant/codex.sock
    control_socket_path: /run/assistant/bridges-control.sock
telegram:
  bots:
    main:
      token_env: TELEGRAM_BOT_TOKEN
      admin: true
  connections:
    personal:
      bot: main
      chat_id: "123456789"
      allow_admin: true
```

Both flags default to false and accept only YAML booleans. Only configured routes
can issue commands. In a group or forum topic, permission applies to everyone on
that connection. Topic matching still applies before command handling.

An admin-enabled bot registers `/new_session`, `/model`, and `/thinking` through
`setMyCommands` when polling starts, replacing its default command menu. It also
registers chat-scoped commands for each connection with `allow_admin: true` and
sets the command menu button in private chats. Chats shared by multiple topic
connections are configured once; Telegram has no per-topic command menu. Execution
still checks the specific connection's permission. A failed chat setup is logged
without blocking other chats or incoming polling. Disabling admin blocks
execution immediately after restart; Telegram may retain a previously registered
menu. A menu entry never grants permission.

Only leading Telegram `bot_command` entities are interpreted as commands, including
commands addressed to the current bot by username. Text quoted inside a message,
captions and model-produced text cannot invoke the control channel. The original
`/new` command is not an alias. Recognized admin commands are consumed by Bridges,
including when disabled or unavailable, and never become Codex prompts.

- `/new_session` accepts no arguments. The owning application creates and saves a
  loaded session using its normal defaults. Bridges verifies it and moves only the
  requesting connection's focus. It does not delete the previous conversation.
- `/model` lists the current session's model and available models. `/model <model>`
  validates and saves a setting for the next turn in that session.
- `/thinking` lists current and supported reasoning levels for the selected model.
  `/thinking <level>` validates and saves the setting for the next turn.

Commands and focus/delivery share the connection lock. Changes never mutate a
running turn. If a session receives input while active, it is steered normally;
new settings apply when its next idle turn starts. The application owns settings
validation, persistence, and model catalogs, so available values are not hardcoded
in Telegram menus. Starting a fresh session does not inherit session overrides.

## Application socket contract

The application creates a local Unix stream socket at `control_socket_path` in a
private runtime directory. Protect it using directory permissions (0700) and a
socket mode such as 0600, granting access only to the application and Bridges.
This endpoint must not be exposed over TCP. Filesystem access is authentication.

Bridges opens a fresh connection for each request and writes one UTF-8 JSON line:

```json
{"connection_id":"personal","thread_id":"current-thread","command":"model","argument":"model-id"}
```

`command` is `new_session`, `model`, or `thinking`; `argument` is an empty string
for a query. Respond with one bounded JSON line:

```json
{"thread_id":"current-thread","message":"Model saved. Applies on the next turn."}
```

Messages are user-facing plain text (1–4096 characters), sent without Markdown
parsing. For `/new_session`, return a different, already loaded `thread_id` after
saving application state. Settings commands must return the original thread ID.
The application may reject a command with `{"error":"User-facing reason."}`.
Bridges verifies a new session before persisting its focus mapping. A control or
routing failure leaves the old route and reports that state should be checked
before retrying; the application may already have committed its operation.

Before submitting each idle turn, Bridges asks the owning application for its
saved overrides using an internal request:

```json
{"connection_id":null,"thread_id":"current-thread","command":"turn_settings","argument":""}
```

Respond with the same thread ID, a message, and `turn_options`, containing only
optional `model` and `effort` strings:

```json
{"thread_id":"current-thread","message":"Session settings.","turn_options":{"model":"model-id","effort":"medium"}}
```

Bridges applies these to `turn/start`, never `turn/steer`. This avoids ineffective
`thread/resume` overrides on an already-loaded thread and restores desired settings
after Bridges restarts. Applications without a control socket retain existing
delivery behavior. With a configured endpoint, unavailability prevents a new turn
from silently running with incorrect settings.

Exchanges have a 15-second deadline and a 64 KiB response limit. Failed or uncertain
mutations are not retried. A lost acknowledgement can mean the operation happened;
query state before repeating `/new_session`.

## Application-side example

[application_control.py](../examples/application_control.py) provides a reusable
`SessionControl` helper. Copy it into the owning application's source; Bridges
does not import or start it. After the application creates or resumes its session:

```python
control = SessionControl(
    initialized_client.call,
    session_state_path,
    thread_start_defaults,
    startup_prompt,
)
control.loaded(thread_start_or_resume_response)
await control.start("/run/assistant/bridges-control.sock")
```

Pass desired `control.sessions[thread_id]` options to application-owned startup
turns too. On shutdown call `await control.close()` before disconnecting the App
Server client. The helper persists session overrides in the application's session
state file, validates choices through paginated `model/list`, and initializes new
threads using the application's own startup prompt. Keep that prompt responsible
for workspace initialization; Bridges does not inject one.
