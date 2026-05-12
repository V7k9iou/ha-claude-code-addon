# shellcheck shell=sh
# Load the Claude Code add-on environment for *all* login shells, not just
# interactive ones.
#
# ~/.claude_env is (re)generated from the add-on options on every container
# start by cont-init.d/04-claude-env.sh and carries:
#   CLAUDE_CONFIG_DIR        — so Claude Code reads /data/claude/settings.json
#                              (the file staged from claude.permission_mode and
#                              the allow list), not the per-home default
#   ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN — Claude Code auth
#   SUPERVISOR_TOKEN         — bearer for http://supervisor/... and the ha-* helpers
#   USE_BUILTIN_RIPGREP
#
# ~/.bashrc also sources this, but only for *interactive* shells. A headless
# launch — an iOS Shortcut's "Run script over SSH" action running
# `bash -lc '... claude --rc'`, cron, an HA automation — is non-interactive, so
# ~/.bashrc never runs. Sourcing it here, from /etc/profile.d (which a login
# shell, `bash -l`, always reads), makes those launches behave like an
# interactive login: right config dir, working auth, and $SUPERVISOR_TOKEN in
# the environment. The exports are idempotent, so the double-source from an
# interactive login shell is harmless.
if [ -r "${HOME:-/data/home}/.claude_env" ]; then
    . "${HOME:-/data/home}/.claude_env"
fi
