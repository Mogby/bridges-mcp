# Ubuntu VPS deployment

This systemd example runs one Telegram bot and its bridge-owned Codex App Server.
It uses Codex CLI 0.169.0, Python 3.12, and a ChatGPT subscription login. The service
runs as root to use the requested `/root/jarvis_workdir`; Codex commands use its
workspace-write sandbox and automatic approval reviewer.

## Layout

| Path | Purpose |
| --- | --- |
| `/root/jarvis_workdir` | Agent workspace; maintain `AGENTS.md` here |
| `/opt/bridges-mcp` | Application source and `.venv` |
| `/etc/bridges/jarvis.yaml` | Private bridge configuration |
| `/etc/bridges/telegram-token` | Bot token, mode 0600 |
| `/var/lib/bridges/codex` | Dedicated `CODEX_HOME`, login, config, sessions |
| `/var/lib/bridges/state` | Bridge locks and saved chat binding |

The service loads the token through systemd credentials. Do not put it in the
unit, command line, source tree, or YAML. Its MCP endpoint listens only on
loopback and uses a fresh bearer token each time it starts.

## Install

Run as root on the target host. Place the source checkout in `/opt/bridges-mcp`.
Install Python venv support if the distribution does not already provide it.

```sh
npm install -g @openai/codex@0.169.0
install -d -m 700 /root/jarvis_workdir /etc/bridges /var/lib/bridges/codex /var/lib/bridges/state
cd /opt/bridges-mcp
python3 -m venv .venv
.venv/bin/pip install .
install -m 600 deploy/systemd/codex.toml /var/lib/bridges/codex/config.toml
CODEX_HOME=/var/lib/bridges/codex codex login --device-auth
```

For a host already signed into the intended ChatGPT account, provision its
existing login into the dedicated home with restricted permissions instead of
signing in again. Do not copy another person's account credentials.

Securely provision the bot token at `/etc/bridges/telegram-token` (mode 0600).
Copy `jarvis.example.yaml` to `/etc/bridges/jarvis.yaml` (mode 0600), replacing the
chat ID with your private Telegram chat ID. Send the bot `/start` before testing.

Create `/root/jarvis_workdir/AGENTS.md` from `workspace-AGENTS.md` if no instructions
file exists. Preserve this file during deployments; it belongs to the user.

```sh
install -m 644 deploy/systemd/jarvis.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now jarvis.service
systemctl status jarvis.service
journalctl -u jarvis.service -n 30 --no-pager
```

## Workspace and operations

Edit `/root/jarvis_workdir/AGENTS.md` to customize the agent. Restart the service
after editing to reload workspace context. Restarting retains the saved Codex
chat; it does not intentionally create another session.

```sh
systemctl restart jarvis.service
systemctl stop jarvis.service
```

The bridge preserves the existing binding across code updates. Stop the service,
update `/opt/bridges-mcp`, reinstall with `.venv/bin/pip install .`, then restart.
Do not overwrite the workspace, token, private YAML, or service `CODEX_HOME`.
To rotate the token, replace the protected token file and restart the service.

`codex.toml` enables automatic review (`on-request` with reviewer `auto_review`),
network access within workspace-write, live web search, and native image
generation. Other built-in tools retain Codex defaults; account-connected tools
remain governed by the account's permissions. Headless App Server does not supply
the desktop application's local GUI, browser session, or sidebar-management API.
Do not confuse enabled feature flags with availability of those desktop surfaces.
Automatic review can reject requests; it is not unconditional command approval.
Human input requests have no bridge operator UI and can leave a turn pending.

## Verified deployment

On 2026-11-14, Ubuntu 24.04 and Codex 0.169.0 passed:

- App Server effective config: requested workspace, `auto_review`, workspace-write,
  and live web search.
- Telegram inbound message → owned model session → authenticated Bridges MCP
  documentation call → successful Telegram text reply.
- Native shell command, web search, and native image generation saved to disk.
- An explicit escalated, read-only shell probe completed under automatic review.
- Graceful systemd restart, same saved thread ID, App Server ready again, and boot
  enablement. Three subsequent messages received successful model-originated
  Telegram replies through the renewed MCP endpoint. Credential files were mode
  0600.

The live tool inventory included native image generation and web search as well
as account-connected app tools. No attempt was made to exercise every connected
service or grant it new permissions. Runtime logs and test artifacts stay on the
host, outside version control.
