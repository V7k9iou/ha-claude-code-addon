ARG BUILD_FROM=ghcr.io/hassio-addons/base:20.1.0
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LANG=C.UTF-8

# Runtime dependencies
# hadolint ignore=DL3018
RUN apk add --no-cache \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        libgcc \
        libstdc++ \
        nano \
        nodejs \
        npm \
        openssh-client \
        openssh-keygen \
        openssh-server \
        openssh-sftp-server \
        python3 \
        ripgrep \
        shadow \
        sqlite \
        sudo \
        tmux \
        tzdata

# Install Claude Code (npm distribution; per-arch native binary pulled as
# optional dependency for linux-arm64 / linux-x64).
RUN npm install -g @anthropic-ai/claude-code \
    && claude --version

# Use system ripgrep instead of the bundled glibc copy — we're on musl/Alpine.
ENV USE_BUILTIN_RIPGREP=0
# Persist Claude config (settings, login state) under /data so it survives
# add-on rebuilds.
ENV CLAUDE_CONFIG_DIR=/data/claude

# Copy our s6 init scripts, sshd config, helper bins, workspace skeleton
COPY rootfs /

# Make scripts executable. (Doing this in the image so that bits are correct
# even if the source repo loses the +x bit on Windows / weird filesystems.)
RUN chmod a+x \
        /etc/cont-init.d/*.sh \
        /etc/profile.d/*.sh \
        /etc/services.d/sshd/run \
        /etc/services.d/sshd/finish \
        /usr/local/bin/ha-call-service \
        /usr/local/bin/ha-list-entities \
        /usr/local/bin/ha-state \
        /usr/local/bin/ha-token \
        /usr/local/bin/ha-restart \
        /usr/local/bin/ha-check-config

LABEL \
    org.opencontainers.image.title="Home Assistant Add-on: Claude Code" \
    org.opencontainers.image.description="SSH into a container running Claude Code with access to Home Assistant config and APIs." \
    org.opencontainers.image.source="https://github.com/talcum/ha-claude-code-addon" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.authors="David <david.talcum489@silomails.com>"
