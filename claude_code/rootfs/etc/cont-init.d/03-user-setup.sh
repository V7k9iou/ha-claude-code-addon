#!/usr/bin/with-contenv bashio
# ==============================================================================
# Create / configure the SSH login user from add-on options.
#
# Single-operator container — the user gets passwordless sudo. SSH itself
# enforces auth (password and/or authorized_keys).
# ==============================================================================
set -e

declare username
declare password

username=$(bashio::config 'ssh.username')

if [[ -z "${username}" ]]; then
    bashio::exit.nok "ssh.username must be set in the add-on configuration"
fi

if [[ "${username}" == "root" ]]; then
    bashio::exit.nok "ssh.username cannot be 'root' — pick a different name"
fi

# Create the user (or update home dir if user already exists)
HOME_DIR="/data/home"
if ! id "${username}" &>/dev/null; then
    bashio::log.info "Creating user ${username}"
    adduser -D -s /bin/bash -h "${HOME_DIR}" "${username}"
else
    bashio::log.debug "User ${username} already exists"
fi

# Password auth
if bashio::config.has_value 'ssh.password'; then
    password=$(bashio::config 'ssh.password')
    echo "${username}:${password}" | chpasswd
    bashio::log.info "Password auth enabled for ${username}"
else
    # No password set — disable password auth for this user (key-only)
    passwd -l "${username}" >/dev/null 2>&1 || true
    bashio::log.info "Password auth disabled for ${username} (key-only)"
fi

# Authorized keys
mkdir -p "${HOME_DIR}/.ssh"
chmod 700 "${HOME_DIR}/.ssh"
KEYS_FILE="${HOME_DIR}/.ssh/authorized_keys"
: > "${KEYS_FILE}"
if bashio::config.has_value 'ssh.authorized_keys'; then
    # Capture into a variable first; feeding `bashio::config` for an array
    # directly into `< <(...)` under bashio strict mode (errexit/pipefail)
    # silently yields an empty stream, so the loop iterates 0 times and the
    # keys never land in the file.
    keys_out=$(bashio::config 'ssh.authorized_keys')
    while read -r key; do
        [[ -z "${key}" ]] && continue
        echo "${key}" >> "${KEYS_FILE}"
    done <<< "${keys_out}"
    bashio::log.info "$(wc -l < "${KEYS_FILE}") authorized_keys configured"
fi
chmod 600 "${KEYS_FILE}"

# Sanity check — at least one auth method must be available
if ! bashio::config.has_value 'ssh.password' \
   && ! bashio::config.has_value 'ssh.authorized_keys'; then
    bashio::log.fatal "No SSH password or authorized_keys configured — login is impossible"
    bashio::exit.nok
fi

# Passwordless sudo (single-operator trust model)
echo "${username} ALL=(ALL) NOPASSWD: ALL" > "/etc/sudoers.d/${username}"
chmod 440 "/etc/sudoers.d/${username}"

# Make sure the user owns their home dir
chown -R "${username}:${username}" "${HOME_DIR}"

bashio::log.info "User ${username} ready (home: ${HOME_DIR})"
