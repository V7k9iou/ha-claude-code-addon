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

## Drive Claude from your phone (iOS Shortcut)

The add-on ships `tmux`, so you can launch a detached Claude Code session with
[Remote Control](https://code.claude.com/docs/en/remote-control) on and then
drive it from the Claude mobile app. Add a **Run script over SSH** action in
the Shortcuts app (Host = your HA host, Port = `22222`, User = your
`ssh.username`, SSH Key = an ed25519 key whose public half goes in
`ssh.authorized_keys`), with this as the script:

```sh
/usr/bin/tmux has-session -t claude 2>/dev/null || /usr/bin/tmux new-session -d -s claude "bash -lc 'cd /data/home && /usr/local/bin/claude --rc'" >/dev/null 2>&1
```

Running it starts the session if it isn't already up (re-runs are no-ops); a
new Remote Control session appears in the Claude app within a few seconds. You
can also attach interactively from a regular SSH login with `tmux attach -t
claude` (detach with `Ctrl-b d`). Don't add `--dangerously-skip-permissions`
or `--permission-mode …` to a headless launch — set the permission posture via
`claude.permission_mode` / `defaultMode` in `/data/claude/settings.json`
instead (see [`DOCS.md`](./claude_code/DOCS.md)).

See [`claude_code/DOCS.md`](./claude_code/DOCS.md) for full setup instructions.
