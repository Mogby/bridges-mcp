# Independent server and application deployment

Run Bridges as one long-lived HTTP service. Run each application as its own
service; an application launches Codex and decides whether to start or resume
conversations. Application code, credentials, configuration, and instructions
live outside the Bridges repository.

## Bridges service

The service runs `bridges serve --config /absolute/path/config.yaml` with bot
and application bearer-token environment variables. See
[the configuration example](../examples/app-server.yaml). Protect credentials
using your supervisor's secret facility, such as systemd `LoadCredential`.

Use a stable loopback port, a persistent `state_dir`, and application-accessible
download directories. One Bridges process can serve several applications. Give
each a distinct client ID, random bearer token, and App Server Unix socket path.
The server can start with all applications offline. Restarting it preserves focus
and does not restart applications. Configure supervisors independently; a Bridges
stop must not cascade into stopping application services.

## Application service

The application runs its Codex App Server with a protected local socket, for example:

```sh
codex app-server --listen unix:///run/my-assistant/codex.sock
```

The owning application connects using WebSockets over that socket, initializes,
and calls `thread/start` or `thread/resume`. It keeps its controlling connection
open. Bridges uses a separate connection for observations and input delivery.
A private runtime directory should protect the socket.

In the application's own `CODEX_HOME/config.toml`, configure:

```toml
[mcp_servers.bridges]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "ASSISTANT_MCP_TOKEN"
```

Supply the matching token to the application's environment. Model, reasoning,
approval, sandbox, and native-tool configuration also belongs in this Codex home.
The application owns conversation persistence and applying model overrides on
resume. Its normal startup turn can read its AGENTS.md. Bridges does not inject
startup prompts or instructions to select connections.

The model calls `bridges.focus(connection_id)` when application/user instructions
request it. A fresh session replaces the previous target for that connection.
An application restarting into the same loaded session can use existing focus.
If it creates a new session, the old mapping stays unavailable until explicitly
replaced. Incoming messages during unavailability are dropped; no inbox is stored.

## Migrating from the combined runner

The previous `codex:` config, bridge-owned `binding.json`, and `reset-binding`
command are removed. Stop the old combined runner before starting the separate
services, so there is only one Telegram poller.

Move the existing Codex home and chosen conversation ID into the owning
application's configuration/state. Keep workspace files and instructions. Codex
records can contain absolute paths: preserve access to old paths if relocating
existing conversation files. Preserve login credentials only on their existing
host, with restricted permissions.

Start the application and use an explicit focus call to select its session.
Bridges stores targets in `<state_dir>/<instance_id>/focus.json`. Stop the server
before using `reset-focus` to clear all assignments. This command does not alter
application history or restart/create any conversation.
