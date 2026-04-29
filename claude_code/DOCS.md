# Claude Code Add-on — Documentation

## Installation

### Option A: Local add-on

1. Connect to the HA host via the **Samba** add-on or the **SSH & Web
   Terminal** add-on.
2. Copy the `claude_code/` folder into `/addons/claude_code/`.
3. In HA: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**.
4. Scroll to **Local add-ons** and click **Claude Code → Install**.

The first build takes several minutes on a Pi (Node.js + npm install of
Claude Code).

### Option B: Custom repository

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add the GitHub URL of this repo.
3. Find **Claude Code** in the new repository section and install.

## Configuration

### Required

You need at least one SSH auth method **and** one Claude Code auth method:

```yaml
ssh:
  username: claude
  password: ""                 # set this, OR
  authorized_keys:             # set this (or both)
    - ssh-ed25519 AAAAC3...
  port: 22222
claude:
  anthropic_api_key: sk-ant-...   # set this, OR
  oauth_token: ""                 # set this (from `claude setup-token`)
  permission_mode: default
log_level: info
```

### Options reference

| Option | Type | Description |
|---|---|---|
| `ssh.username` | string | Login name. Cannot be `root`. |
| `ssh.password` | password | If set, password auth is enabled for this user. Leave empty to disable. |
| `ssh.authorized_keys` | list of strings | OpenSSH-format public keys, one per entry. |
| `ssh.port` | port | SSH server port (default: 22222). |
| `claude.anthropic_api_key` | password | Direct API key (`sk-ant-...`). Use this **or** `oauth_token`. |
| `claude.oauth_token` | password | Long-lived OAuth token from `claude setup-token` on a desktop with browser access. Use if you have a Pro/Max subscription. |
| `claude.permission_mode` | enum | Default permission posture for Claude Code: `default`, `acceptEdits`, `plan`, `bypassPermissions`. |
| `log_level` | enum | Add-on log verbosity: `trace`, `debug`, `info`, `notice`, `warning`, `error`, `fatal`. |

### Recommended permission modes

For an SSH-into-container workflow where you trust the operator:

- **`default`** — prompts for tool calls. Safe but interactive.
- **`acceptEdits`** — auto-accepts file edits but still prompts for Bash.
  Reasonable middle ground.
- **`bypassPermissions`** — no prompts. Convenient when you're babysitting
  the session; rough if you're stepping away.

### First-time auth

If you set `claude.anthropic_api_key`, Claude Code is ready immediately —
SSH in, run `claude`, and go.

If you'd rather log in with your Claude.ai subscription, leave both auth
fields blank, SSH in, and run:

```sh
claude
/login
```

The login flow uses a one-time code shown in the terminal — no browser
required inside the container. The credential is saved under
`/data/claude/` and survives add-on updates.

For headless workflows, `claude setup-token` on a desktop machine with a
browser will print a long-lived `CLAUDE_CODE_OAUTH_TOKEN` you can paste
into `claude.oauth_token`.

## Connecting

```sh
ssh -p 22222 claude@<your-ha-host>
```

Once in:

```
$ claude
```

The `/workspace` directory has symlinks to all mapped HA paths. The
`CLAUDE.md` file in that directory is loaded by Claude Code as standing
context — it briefs the model on your filesystem layout, the helper
scripts, and the safety rules for this environment.

## What Claude can access

Mapped directories inside the container:

| Path | HA folder | Mode |
|---|---|---|
| `/homeassistant/` | `/config` | rw |
| `/share/` | `/share` | rw |
| `/media/` | `/media` | rw |
| `/addon_configs/` | all add-on configs | rw |
| `/addons/` | local add-ons | ro |
| `/ssl/` | SSL certs | ro |
| `/backup/` | HA backups | ro |
| `/data/` | this add-on's persistent storage | rw |

API access:

- HA Core REST: `http://supervisor/core/api/...`
- HA Core WebSocket: `ws://supervisor/core/websocket`
- Supervisor: `http://supervisor/...` (role: `manager`)

The bearer token is in `$SUPERVISOR_TOKEN` in every login shell.

## Persistence

Only `/data` survives add-on updates. The add-on stores:

- `/data/claude/` — Claude Code config, login state, settings.json
- `/data/ssh/` — SSH host keys (your `known_hosts` fingerprint stays
  stable across updates)
- `/data/home/` — the SSH user's home directory (`.bash_history`, dotfiles)

Anything outside `/data` (or the mapped HA directories) is wiped on every
add-on update.

## Updating

Edit your add-on locally and bump `version:` in `config.yaml`. Reload the
Add-on Store, then click **Update** on the add-on page. The container
rebuilds; `/data` is preserved.

## Safety

Read `/workspace/CLAUDE.md` for the full operator briefing the model gets
about this environment. Highlights:

- Always `ha-check-config` before `ha-restart`.
- Take a Supervisor backup before risky changes.
- Don't hand-edit `.storage/` files unless HA Core is stopped.
- Open the recorder DB read-only.
- Treat `$SUPERVISOR_TOKEN` and `secrets.yaml` as sensitive.

## Uninstalling

Stopping the add-on stops sshd. Uninstalling removes the container but
**preserves `/data`** by default — your Claude login and SSH host keys
survive. Delete the `/addons/claude_code/` folder (and the data directory
under `/mnt/data/supervisor/addons/data/<slug>/` if you want a fully
clean slate) to remove all traces.

## Troubleshooting

**Add-on won't start after install**
Check the Log tab. Common causes:
- Both `ssh.password` and `ssh.authorized_keys` empty → no auth method →
  the add-on refuses to start.
- `ssh.username: root` → not allowed; pick something else.

**SSH connection drops**
The keepalive interval is 60s; if your firewall is more aggressive, set
`ServerAliveInterval` on the client side.

**Claude Code says "not authenticated"**
Either set `claude.anthropic_api_key`, set `claude.oauth_token`, or run
`/login` inside an SSH session and complete the device-code flow.

**Builds take forever on a Pi**
First build pulls Node.js + npm-installs Claude Code. Expect 5–15 minutes
on a Pi 4. Subsequent updates only rebuild what changed.
