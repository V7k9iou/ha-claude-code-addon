# Changelog

## 0.1.6 — fix invalid default for `claude.effort`

- FIX: `claude.effort` shipped with an empty-string default (`""`) but its
  schema only accepted `low` / `medium` / `high` / `xhigh`, so the add-on
  failed config validation on a fresh install ("value must be one of …").
- The enum now includes a `default` sentinel (the new default value) which
  leaves `CLAUDE_CODE_EFFORT_LEVEL` unset, so Claude Code picks its own
  effort. The field stays visible in the Configuration tab.

## 0.1.5 — model and effort options

- New `claude.model` option — set Claude Code's model from the Configuration
  tab: an alias (`opus` / `sonnet` / `haiku`) or a full ID (`claude-opus-4-7`,
  …). Written into `/data/claude/settings.json` as the `model` key. Empty ⇒
  Claude Code's default.
- New `claude.effort` option — reasoning effort level (`low` / `medium` /
  `high` / `xhigh`). Exported as `CLAUDE_CODE_EFFORT_LEVEL` in `~/.claude_env`
  rather than written to `settings.json`'s `effortLevel`: the env var also
  overrides Opus 4.7's one-time "launch effort" pin (xhigh on the first
  session), which the `effortLevel` key by itself doesn't — so a bare
  `effortLevel` would be silently ignored on Opus 4.7 until you changed effort
  manually once. Empty ⇒ the model's default.
- Both are **re-applied on every container start** (`~/.claude_env` is rebuilt
  from the options each boot; `04-claude-env.sh` re-syncs the `model` key in
  `settings.json` via `jq`) — unlike `claude.permission_mode`, which is only
  consulted when `settings.json` is first staged. So you can change model/effort
  on an existing install and just restart the add-on; no `/data` wipe. Leaving
  an option empty leaves the corresponding key/var untouched, so a hand-set
  value survives.
- DOCS: new "Model and effort" section; the two new options in the reference
  table and the example config; a note that since v0.1.4 a headless launch
  reads `/data/claude/settings.json` (not the per-home `~/.claude/settings.json`),
  so the add-on options actually reach it now — and anything hand-edited into
  `~/.claude/settings.json` on an older version stops being read after the
  update.

## 0.1.4 — load the add-on env in headless launches

- Add `/etc/profile.d/01-claude-env.sh`, which sources `~/.claude_env` for all
  login shells — not just interactive ones. Without it, a headless launch (the
  `bash -lc '... claude --rc'` an iOS Shortcut / cron uses) is non-interactive,
  so `~/.bashrc` never runs and `~/.claude_env` is never loaded. The
  consequences were silent and confusing: `claude` ran with **no
  `CLAUDE_CONFIG_DIR`**, so it read settings from the per-home default
  (`~/.claude/settings.json`) instead of the add-on's staged
  `/data/claude/settings.json` — i.e. the `claude.permission_mode` option (and
  any edits to that file) had no effect on shortcut-launched sessions; it also
  had no Claude Code auth env and no `$SUPERVISOR_TOKEN` in the environment, so
  it only worked at all if a `claude /login` credential happened to be in the
  default config dir, and the `ha-*` helpers / `curl http://supervisor/...`
  needed a manual `. ~/.claude_env` prepended. With this file, headless
  launches behave like an interactive login: right config dir, working auth,
  `$SUPERVISOR_TOKEN` available.
- `chmod a+x` `/etc/profile.d/*.sh` in the image build (alongside the
  cont-init scripts) so the bit is correct regardless of how the repo was
  checked out.
- DOCS: hardened the "don't use Bypass Permissions mode headless" warning — it
  applies to `permission_mode: bypassPermissions` / `"defaultMode":
  "bypassPermissions"` in settings, not just the `--dangerously-skip-permissions`
  flag. Both pop a blocking "Bypass Permissions mode" confirmation on every
  fresh session, so a headless `--rc` launch hangs on the dialog and never
  appears in the mobile app. For headless, use `default` / `acceptEdits` /
  `plan` and widen the `allow` list to taste.

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
