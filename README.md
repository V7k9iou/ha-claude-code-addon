# Home Assistant Add-on: Claude Code

A Home Assistant add-on that runs an SSH-accessible container with [Claude
Code](https://www.claude.com/product/claude-code) installed. SSH in from any
machine and use Claude as an interactive operator for your Home Assistant
instance — creating automations, scenes, dashboards, querying the recorder
database, generating reports, troubleshooting integrations, etc.

This is a personal-use add-on. The container is granted broad access to your
Home Assistant configuration and Supervisor API, so only install it if you
trust the operator on the other end of the SSH session (i.e. yourself).

## Add-ons in this repository

- **[claude_code](./claude_code)** — Run Claude Code via SSH inside a Home
  Assistant add-on container.

## Installation as a custom repository

In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
then add this repository's URL.

## Installation as a local add-on

Copy the `claude_code/` folder into `/addons/claude_code/` on your Home
Assistant OS host (via the Samba or SSH add-on). Then in the Add-on Store,
click **⋮ → Check for updates** to make it appear under "Local add-ons".

See [`claude_code/DOCS.md`](./claude_code/DOCS.md) for full setup instructions.
