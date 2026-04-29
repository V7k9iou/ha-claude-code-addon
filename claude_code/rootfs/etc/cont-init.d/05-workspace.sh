#!/usr/bin/with-contenv bashio
# ==============================================================================
# Lay out /workspace as a convenience landing pad with symlinks into the
# mapped Home Assistant directories + a CLAUDE.md briefing the operator
# (and Claude itself) on what's where.
# ==============================================================================
set -e

declare username
username=$(bashio::config 'ssh.username')

mkdir -p /workspace

# Symlink mapped directories into /workspace
for dir in homeassistant share media addons addon_configs ssl backup; do
    src="/${dir}"
    dst="/workspace/${dir}"
    if [[ -d "${src}" ]] && [[ ! -e "${dst}" ]]; then
        ln -s "${src}" "${dst}"
    fi
done

# CLAUDE.md is shipped under /workspace/CLAUDE.md by the rootfs COPY in the
# Dockerfile; nothing to do here for that.

# Provide a .bashrc that lands the user in /workspace and sources the env file
HOME_DIR="/data/home"
BASHRC="${HOME_DIR}/.bashrc"
if ! grep -q "claude-addon-bashrc-marker" "${BASHRC}" 2>/dev/null; then
    cat >> "${BASHRC}" <<'EOF'

# claude-addon-bashrc-marker — managed by the Claude Code add-on
if [ -f "$HOME/.claude_env" ]; then
    . "$HOME/.claude_env"
fi

# Land in the workspace by default
if [ -d /workspace ] && [ "$PWD" = "$HOME" ]; then
    cd /workspace
fi

# Show the welcome banner once per session
if [ -f /etc/motd-claude ] && [ -z "$CLAUDE_ADDON_MOTD_SHOWN" ]; then
    cat /etc/motd-claude
    export CLAUDE_ADDON_MOTD_SHOWN=1
fi

# Aliases
alias ll='ls -lah'
alias ha='curl -sSL -H "Authorization: Bearer $SUPERVISOR_TOKEN"'
EOF
    chown "${username}:${username}" "${BASHRC}"
fi

# Workspace itself stays root-owned (the symlinks point at root-owned mounts)
# but the user can write into the targets via the rw map flags.

bashio::log.info "Workspace prepared at /workspace"
