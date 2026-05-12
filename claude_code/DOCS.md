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
  permission_mode: acceptEdits
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
| `claude.permission_mode` | enum | Permission posture for Claude Code: `default`, `acceptEdits`, `plan`, `bypassPermissions`. Default: `acceptEdits`. Only takes effect on a fresh install (or a reinstall with a wiped `/data`) — see "Changing the permission mode later" below. |
| `log_level` | enum | Add-on log verbosity: `trace`, `debug`, `info`, `notice`, `warning`, `error`, `fatal`. |

### Recommended permission modes

For an SSH-into-container workflow where you trust the operator:

- **`default`** — prompts for every tool call. Safest, but interactive enough
  to be annoying for routine HA work.
- **`acceptEdits`** — auto-accepts file edits but still prompts for Bash
  commands outside the staged `allow` list. **This is the add-on's default**:
  a reasonable middle ground for a single trusted operator.
- **`bypassPermissions`** — no prompts at all. Convenient when you're
  babysitting the session; rough if you step away, given this container's rw
  access to `/homeassistant` and the Supervisor `manager` API.

### Changing the permission mode later

The `claude.permission_mode` option is only consulted when the add-on **stages
`/data/claude/settings.json`**, which it does only if that file doesn't already
exist (so it never clobbers a settings file you've customised). That means:

- **Fresh install, or reinstall after wiping `/data`** — `settings.json` is
  written from the option. Set it in the Configuration tab before/at first
  start.
- **Existing install** — `/data/claude/settings.json` already exists, so
  changing the Configuration-tab option does nothing. Edit the file directly:
  set `"defaultMode"` to `"default"` / `"acceptEdits"` / `"bypassPermissions"`,
  then restart the add-on (or `tmux kill-session -t claude` and relaunch) so a
  new session picks it up. The file is under `/data`, so the edit survives
  add-on updates. You can also pre-bless recurring actions by adding patterns
  to the `permissions.allow` list there instead of loosening the mode.

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

## Headless launches (iOS Shortcut, cron, automation)

If you want to start a Claude Code session from something *other* than an
interactive SSH terminal — e.g. an iOS Shortcut that triggers a [Remote
Control](https://code.claude.com/docs/en/remote-control) session you can
drive from the Claude mobile app — you need to detach `claude` from the
SSH connection so it keeps running after the connection drops. `tmux` is
installed in the container for exactly this.

The recipe (also works as the script body of an iOS Shortcuts "Run script
over SSH" action):

```sh
/usr/bin/tmux has-session -t claude 2>/dev/null \
  || /usr/bin/tmux new-session -d -s claude \
       "bash -lc 'cd /data/home && /usr/local/bin/claude --rc'" \
       >/dev/null 2>&1
```

What each piece does:

- `tmux has-session -t claude || tmux new-session -d -s claude '...'` —
  start the session if it isn't already running, otherwise no-op. Re-runs
  of the shortcut won't pile up duplicate sessions.
- `bash -lc '...'` — a **login** shell (`-l`). That matters: a login shell
  reads `/etc/profile`, which runs `/etc/profile.d/01-claude-env.sh`, which
  sources `~/.claude_env`. That's what gives the headless `claude` its
  `CLAUDE_CONFIG_DIR` (so it reads `/data/claude/settings.json` — the file
  staged from `claude.permission_mode` and the `allow` list — rather than the
  per-home default), its Claude Code auth, and `$SUPERVISOR_TOKEN`. Drop the
  `-l` and you lose all three. (`~/.bashrc` only sources `~/.claude_env` for
  *interactive* shells, which a `bash -c` from a Shortcut isn't — added in
  v0.1.4.)
- `--rc` — start with [Remote
  Control](https://code.claude.com/docs/en/remote-control) so the session
  shows up in the Claude mobile app and at claude.ai/code. (Equivalent to
  flipping "Enable Remote Control for all sessions" in `/config`.)

Attach to the running session interactively from a regular SSH login:

```sh
tmux attach -t claude
```

Detach again with `Ctrl-b d` — the session keeps running.

> ⚠️ **Don't use Bypass Permissions mode with a headless launch.** Whether you
> set it via `--dangerously-skip-permissions` on the command line *or* via
> `permission_mode: bypassPermissions` / `"defaultMode": "bypassPermissions"`
> in settings, Claude Code shows a blocking "WARNING: Claude Code running in
> Bypass Permissions mode" confirmation on **every** fresh session, with
> "No, exit" preselected. A headless `--rc` launch has no one at the terminal
> to choose "Yes" — so the session hangs on the dialog, never starts Remote
> Control, and never appears in the Claude mobile app. (If you ever see "the
> shortcut runs but no session shows up," `tmux attach -t claude` and you'll
> find it sitting on this dialog.) For a headless launch, use `default`,
> `acceptEdits`, or `plan` — set via the `claude.permission_mode` option or
> `"defaultMode"` in settings; those don't pop a launch-time dialog. To cut
> down on prompts in `acceptEdits`/`default` without going to bypass, add the
> command patterns you keep approving to the `permissions.allow` list in
> settings.

### Setting up an iOS Shortcut

1. Add the **Run script over SSH** action.
2. **Host**: your HA host's hostname or IP (not the add-on's internal
   `172.30.x.x`).
3. **Port**: `22222` (or whatever you set `ssh.port` to).
4. **User**: matches `ssh.username` in the add-on options.
5. **SSH Key**: pick or generate an ed25519 key in Shortcuts; the
   **public** half goes in `ssh.authorized_keys` in the add-on
   configuration. The Shortcuts SSH-key picker offers a "Share" option to
   copy the public key.
6. **Script**: paste the tmux line above.

After running the shortcut, the new session appears in the Claude mobile
app within a few seconds.

The add-on pre-accepts the workspace trust dialog for `/data/home` and
`/workspace` at first start (see
`rootfs/etc/cont-init.d/06-claude-state.sh`), so headless launches don't
get stuck at the "Do you trust this folder?" prompt.

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
