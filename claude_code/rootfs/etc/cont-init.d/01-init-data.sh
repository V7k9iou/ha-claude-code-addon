#!/usr/bin/with-contenv bashio
# ==============================================================================
# Prepare the persistent /data layout. /data survives add-on rebuilds; anything
# else (/etc, /home, ...) is wiped on every restart.
# ==============================================================================
set -e

mkdir -p /data/ssh
mkdir -p /data/claude
mkdir -p /data/home

# Tighten perms on the Claude config dir so login credentials aren't world-readable
chmod 700 /data/claude

bashio::log.info "Persistent /data directories ready"
