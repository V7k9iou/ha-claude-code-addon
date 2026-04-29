#!/usr/bin/with-contenv bashio
# ==============================================================================
# Persistent SSH host keys: generate once into /data/ssh, then copy into
# /etc/ssh on every boot. This means the SSH host fingerprint stays stable
# across add-on updates so clients don't see "remote host key changed" warnings.
# ==============================================================================
set -e

cd /data/ssh

if [[ ! -f ssh_host_rsa_key ]]; then
    bashio::log.info "Generating SSH host keys (one-time)..."
    ssh-keygen -t rsa     -b 4096 -f ssh_host_rsa_key     -N "" -q
    ssh-keygen -t ed25519         -f ssh_host_ed25519_key -N "" -q
    ssh-keygen -t ecdsa   -b 521  -f ssh_host_ecdsa_key   -N "" -q
fi

mkdir -p /etc/ssh
cp /data/ssh/ssh_host_*_key      /etc/ssh/
cp /data/ssh/ssh_host_*_key.pub  /etc/ssh/
chmod 600 /etc/ssh/ssh_host_*_key
chmod 644 /etc/ssh/ssh_host_*_key.pub

bashio::log.info "SSH host keys installed"
