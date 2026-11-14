# Deployment configuration

Bridges is a reusable communication dependency. A bot or application that uses
it owns its service unit, launcher, workspace, credentials, and Codex home.
Keep application-specific deployment files outside this repository and outside
the installed Bridges package.

The application launches `bridges serve --config /absolute/path/bridges.yaml`
and supplies bot token environment variables. Bridges launches its owned Codex
App Server child with the inherited `CODEX_HOME`; it does not set the model.

Configure model, reasoning effort, approvals, sandbox policy, and native tools
in the application's Codex `config.toml`. Keep connection routing, the Codex
executable/workspace, and bridge state/download locations in the application's
Bridges YAML. See [the generic configuration example](../examples/app-server.yaml).

Use a private persistent Codex home and bridge state directory. Preserve both
across upgrades to retain the login and chat binding. Codex session records can
contain absolute paths, so preserve access to those paths if relocating an
existing deployment.

A service supervisor should set the application working directory, provide its
credentials without placing them on the command line, and restart the process
on failure. Stop the service before resetting its binding. A reset creates a new
chat; it does not remove workspace files or change Codex configuration.

The current bridge resumes a saved chat without explicit model overrides.
Changing the default model in Codex configuration does not constitute a request
to switch an existing chat's saved model. Starting a new chat uses the configured
default; switching an existing chat requires an explicit App Server override.
