# Configuration and upgrades

Bridges connects to independently provided harness endpoints. Backend installation,
process supervision, login, native tools, and sandbox/approval policy belong to the
chosen harness and deployment environment. They are outside Bridges's scope.

## Clients, sessions, and connections

`clients` configure adapters and authenticated MCP access. The bundled
`codex-app-server` adapter uses a protected local Unix WebSocket. Restrict socket
access through filesystem permissions and supply the same MCP bearer token to the
harness and Bridges using environment variables. No harness process is launched by
Bridges.

`sessions` define stable logical groups with `client`, absolute `cwd`, and optional
`model`/`effort` defaults. Every connection references a configured `session_id`.
Multiple connections may share one session; history and settings are shared, while
transport settings and admin permissions remain per connection.

`<state_dir>/sessions.json` stores the group's nullable backend pointer
and saved overrides. A logical ID is independent of any harness-native session or
conversation ID. Runtime changes override configured defaults until cleared.

Startup makes no conversations and sends no initialization prompts. Connections
are resolved on demand. Temporary failures preserve pointers. An uncertain creation
marker blocks automatic retry until the user checks state and explicitly requests
a new conversation.

## Adapter boundary

Adapters implement the `Backend` contract in `transports/base.py` and are registered
in `transports/__init__.py`. They normalize creation responses and `ModelInfo` catalogs,
classify confirmed missing conversations, and expose `Transport` lifecycle/delivery.
Harness-specific RPC methods, errors, and response shapes belong in the adapter.
The session manager does not interpret native protocol fields.

## Upgrading from 0.3.x

Back up configuration and routing state. Add logical sessions and each connection's
`session_id`. While stopped, import existing conversation pointers into:

```json
{"sessions":{"personal":{"thread_id":"existing-conversation-id","model":"model-id","effort":"medium"}}}
```

Connections grouped together must agree on backend and initial conversation. Import
existing desired settings rather than resetting them. A null pointer deliberately
requests automatic creation on the next incoming message.

The separate application-control endpoint and agent-controlled focus API are removed.
Remove `control_socket_path` from client configuration and obsolete focus instructions
from workspace guidance. Preserve conversation history and credentials when changing
backend deployment. Bridges refuses to start with only legacy `focus.json` state and
no migrated `sessions.json`; it does not silently discard an existing pointer.

After verification, archive obsolete state. To deliberately clear shared pointers
while stopped, use `bridges reset-sessions --config ...`. This does not delete backend
conversation history. There is no `reset-focus` command.

## Storage layout

All state belongs directly to the configured `state_dir`: `sessions.json`,
`downloads/`, `cron.sqlite`, `context.sqlite`, and `server.lock`. Separate processes use separate state directories
and HTTP ports. Downloads always use `<state_dir>/downloads`.

When upgrading a nested state layout, stop Bridges, move the chosen
`<old-directory>/sessions.json` into `state_dir`, and remove the old instance
metadata field from its JSON wrapper, leaving only `{"sessions": {...}}`. Move
existing downloads into `state_dir/downloads`, then remove the obsolete
`instance_id` and `download_dir` configuration fields. Preserve all session
pointers and settings. Restart after migration; unmigrated nested session files
are detected instead of being silently treated as an empty installation.

## Scheduler storage

No external scheduler service is required. Set `cron.timezone` to an IANA timezone;
the default is `UTC`. Back up `cron.sqlite` while Bridges is stopped, together with
configuration and `sessions.json`. It contains prompts and trusted serialized job
records: restrict access and never load an untrusted database. One process owns
each state directory. Startup preserves job IDs and paused status, applies the
configured timezone, and advances active jobs past downtime.

## Context tracking storage

`context.sqlite` stores last-reported usage and deduplicated completed compaction
item IDs, keyed by backend and thread. Back it up while Bridges is stopped. The
file is created automatically with mode `0600`; it contains counters and timestamps,
not conversation content. Deleting it loses observed counts and cached usage but
does not affect session routing or backend history. Cached readings become stale
on restart. See [[context|Context usage]] for display and coverage limits.
