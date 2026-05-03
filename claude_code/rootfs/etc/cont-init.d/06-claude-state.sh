#!/usr/bin/with-contenv bashio
# ==============================================================================
# Pre-accept the workspace trust dialog for the directories the operator is
# likely to launch claude from. Without this, headless launches (iOS Shortcut,
# cron, automation) get stuck forever at the "Do you trust this folder?" prompt
# because there's no human at the terminal to press 1.
#
# Idempotent: only sets the trust flag, preserves everything else; safe to run
# on every container start.
# ==============================================================================
set -e

declare username
username=$(bashio::config 'ssh.username')

HOME_DIR="/data/home"
CLAUDE_JSON="${HOME_DIR}/.claude.json"

mkdir -p "${HOME_DIR}"

python3 - "${CLAUDE_JSON}" <<'PYEOF'
import json, os, sys

path = sys.argv[1]
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError):
        data = {}
else:
    data = {}

projects = data.setdefault("projects", {})
for d in ("/data/home", "/workspace"):
    proj = projects.setdefault(d, {})
    proj["hasTrustDialogAccepted"] = True

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
PYEOF

chown "${username}:${username}" "${CLAUDE_JSON}"
chmod 600 "${CLAUDE_JSON}"

bashio::log.info "Pre-accepted Claude workspace trust for /data/home and /workspace"
