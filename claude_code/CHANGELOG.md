# Changelog

## 0.1.3 — default permission mode + iOS Shortcut docs

- Default `claude.permission_mode` is now `acceptEdits` (was `default`), so a
  fresh install auto-accepts file edits and only prompts for Bash commands
  outside the staged `allow` list — much less interactive friction for the
  trusted-operator workflow this add-on is built for, without removing the
  checkpoint on shell commands. Override per-install in the Configuration tab
  (`default` / `acceptEdits` / `plan` / `bypassPermissions`).
- Note: `04-claude-env.sh` still only stages `settings.json` if it's missing,
  so an existing `/data/claude/settings.json` keeps whatever `defaultMode` it
  already has — this change affects fresh installs (and reinstalls with a wiped
  `/data`). To change the mode on an existing install, edit
  `/data/claude/settings.json` directly.
- README: documented the iOS Shortcut "Run script over SSH" recipe (the
  detached-`tmux` + `claude --rc` headless launch) alongside the existing
  coverage in `DOCS.md`.

## 0.1.2 — runtime additions

- Bake `python3` into the image so it's available from first boot, instead
  of needing a manual `apk add python3` after every add-on rebuild (#3)

## 0.1.1 — bug fixes

- Fix `ssh.authorized_keys` not being written to `/data/home/.ssh/authorized_keys`
  on container start, leaving SSH key auth silently broken (#1)

## 0.1.0 — initial release

- OpenSSH server (port 22222 by default) with password and key-based auth
- Claude Code installed via npm; `linux-arm64` and `linux-x64` supported
- Persistent SSH host keys in `/data/ssh/`
- Persistent Claude config in `/data/claude/` (`CLAUDE_CONFIG_DIR`)
- HA Core REST + Supervisor API access (`hassio_role: manager`)
- Helper scripts: `ha-state`, `ha-call-service`, `ha-list-entities`,
  `ha-check-config`, `ha-restart`, `ha-token`
- `/workspace/` landing dir with symlinks to all mapped HA paths
- `/workspace/CLAUDE.md` operator briefing for Claude Code
- AppArmor disabled by default (TODO: ship a tightened profile)
