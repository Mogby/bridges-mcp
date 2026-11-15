# Shared-session design

Codex App Server is an independently managed daemon. Bridges connects over a
configured local Unix WebSocket and never manages the daemon's process.

Connections reference a logical `session_id`. Configuration binds that session to
one authenticated client/backend and workspace. `sessions.json` stores one nullable
Codex `thread_id` and desired model/effort overrides for each logical session. This
is the sole routing pointer; Codex stores the conversation itself. Logical IDs are
separate from Codex's own `sessionId` and `thread.id` fields.

A dispatcher feeds one bounded queue per logical session. Each consumer holds that
session's lock across resolution, admin commands, and input submission. Connections
sharing a session cannot race creation or replacement. Different sessions do not
wait on one another's network requests. Queue overflow and unavailable delivery
have sanitized diagnostics. There is no durable inbox.

Resolution creates null pointers, resumes unloaded conversations, and replaces
only confirmed missing thread pointers. Provider mismatches, malformed IDs,
permissions, storage errors, timeouts, and disconnections preserve the pointer.
Confirmed absence is classified from specific installed Codex resume errors, never
from arbitrary “not found” substrings. Unknown protocol errors fail conservatively.

Creation persists a pending marker before the mutating RPC. Successful creation
atomically replaces the record before input is delivered. Definite RPC rejection
clears the pending marker; uncertain outcomes retain it and block automatic retry.
Explicit `/new_session` can retry after the user inspects Codex state. Atomic file
replacement, file synchronization, and directory synchronization provide durable
acknowledgement. Startup refuses unmigrated legacy focus state or corrupt records.

There is no initialization prompt. `thread/start` creates an empty conversation;
only user input invokes `turn/start`. `thread/resume` subscribes Bridges to lifecycle
notifications. Observations reconcile state without resuming historical conversations
speculatively. All RPC traffic shares one authenticated-by-filesystem Unix connection
per configured backend. Bridges never changes login, sandbox, or approval policy.

Idle turns receive saved model/effort options. Active turns receive `turn/steer` with
an expected turn ID; settings take effect on the next idle turn. A definite stale-turn
rejection allows one bounded reconciliation retry. Ambiguous submissions are never
retried. Stale completion events cannot end a newer turn's typing ownership.

Telegram routes are trusted listener metadata, separate from rendered input text.
Admin commands require both bot and connection permissions. A new thread replaces
the shared pointer for the entire group and stops its displaced typing refreshes.
Replies choose explicit destinations and never fan out. Outbound MCP authorization
uses bearer-selected backend identity and host-provided calling-thread metadata;
caller-supplied tool arguments cannot change either identity.

Native daemon installation, credential provisioning, supervisor configuration, and
workspace instructions are deployment concerns. Custom application runners, separate
control sockets, focus tools, startup prompts, Discord, and cron are not part of
Bridges. Future transport adapters can route into the same session manager.
