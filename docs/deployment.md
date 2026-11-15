# Daemon deployment and migration

Manage Codex independently from Bridges. Provision its ChatGPT login, configuration,
workspace, MCP bearer token environment, and protected Unix socket. The installed
CLI's `codex app-server daemon bootstrap` installs native daemon management; inspect
its output for the selected backend and socket path. On Linux the backend may be
PID-based, so use your supervisor to invoke native start/stop on boot/shutdown.
No custom Python application runner is required.

In the daemon's Codex configuration:

```toml
[mcp_servers.bridges]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "CODEX_MCP_TOKEN"
```

Supply that token securely to both services. Configure a matching `clients` entry
in Bridges with the daemon socket path. Define logical `sessions` selecting the
client and absolute workspace, then set every connection's `session_id`.
Model/effort defaults can be configured on a logical session; sandbox and approval
policies remain in the daemon's own configuration. Credentials never go into YAML.

Bridges starts with no daemon available and creates no threads until input or an
admin command needs one. It resumes saved threads on demand after either service
restarts. Stopping Bridges disconnects its clients and typing tasks, never Codex.

## Migrating from 0.3.x

1. Back up configuration, legacy focus state, application session settings, and
   application code before changing the running services.
2. Add a logical session for each intended sharing group and bind connections via
   `session_id`. Connections grouped together must agree on backend and initial thread.
3. While Bridges is stopped, write `<state_dir>/<instance_id>/sessions.json`:

```json
{"instance_id":"main","sessions":{"personal":{"thread_id":"existing-codex-thread-id","model":"model-id","effort":"medium"}}}
```

4. Import the pointer from legacy `focus.json` and the desired settings from the
   owning application's saved state. Preserve the existing Codex home and history
   while testing. A null pointer deliberately requests automatic creation.
5. Remove `control_socket_path` from client configuration. Start Bridges 0.4.x,
   verify six MCP tools and shared-session docs, and test model/thinking queries.
6. Replace the custom application runtime with the native daemon. Point the Bridges
   client at its socket and verify the existing thread resumes and messages/replies work.
7. Remove obsolete focus/control instructions, custom runner code, legacy service
   units, old session pointer, and legacy focus state after verification. Keep backups
   separate from active configuration. Preserve credentials and conversation history.

Bridges refuses to start with only a legacy `focus.json` and no new session state;
it never silently treats existing routing as an empty installation. `focus` and
`reset-focus` are removed. To deliberately clear all shared pointers while stopped,
use `bridges reset-sessions --config ...`; this does not delete Codex history.

If relocating a Codex home, account for stored absolute paths and symlinks. Verify
resumption before removing the old path. Conversation data must not be discarded
as part of removing an application wrapper.
