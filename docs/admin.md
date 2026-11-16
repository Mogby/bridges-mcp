# Telegram admin commands

Set bot `admin: true` and connection `allow_admin: true`. Both default to false.
The connection must reference a configured logical `session_id`. Permission applies
to everyone on that route, including group members. Topic matching occurs before
command handling.

At startup an admin bot registers its default commands. Each permitted chat gets
chat-scoped commands; private chats also get a `commands` menu button. Multiple topic
connections in the same chat are configured once. Telegram has no per-topic menu;
execution still checks the specific connection. A failed chat setup is logged and
does not block other chats or incoming polling. Disabling permission blocks execution,
although Telegram can retain old menu entries. A menu entry does not grant permission.

Only leading Telegram `bot_command` entities are interpreted. Addressed commands
must match the bot username when known. Captions, quoted text, and model output
cannot invoke the admin channel. Recognized denied/unavailable commands are consumed
and never become agent prompts. `/new` is not an alias.

- `/context`: no arguments. Shows the cached [context usage](context.md) and observed
  compaction count without loading a thread or starting a model turn.
- `/new_session`: no arguments. Creates an empty thread, saves its pointer for the
  entire logical session, and leaves old history intact. No initialization turn.
- `/model`: current shared model and available choices. `/model <model>` saves a valid
  model; unsupported current reasoning effort switches to the model's default.
- `/thinking`: current and available reasoning levels for the selected model.
  `/thinking <level>` validates and saves the level.

Choices come from the backend's normalized model catalog. Settings persist in shared
session state and apply at the next idle turn, never by steering a running turn.
They also apply to new threads created within the same logical session. All commands
share the session lock with incoming delivery. Replies go only to the requesting
connection. No admin MCP tools or separate application control socket exist.

If creation has an uncertain outcome, inspect backend state before explicitly using
`/new_session` again. Bridges blocks automatic retry to avoid duplicate conversations.
