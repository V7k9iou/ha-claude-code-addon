ARG BUILD_FROM=ghcr.io/hassio-addons/base:20.1.0
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt

# Python runtime in a venv. The whole dependency tree (20 packages, incl.
# aiohttp, pycryptodome and Pillow) ships musllinux wheels for Alpine on
# aarch64 and amd64, so no compiler is installed: --only-binary makes pip fail
# loudly instead of trying to build if that ever changes. (Installing gcc /
# musl-dev used to be a "safety net" and broke the build when Alpine's musl-dev
# moved ahead of the base image's pinned musl.)
# hadolint ignore=DL3018
RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt /root/.cache

COPY rootfs /

RUN chmod a+x \
        /etc/services.d/companion/run \
        /etc/services.d/companion/finish

LABEL \
    org.opencontainers.image.title="Home Assistant Add-on: Roborock Local Companion" \
    org.opencontainers.image.description="Local-only map switching and status for a Matter-paired Roborock, via MQTT discovery." \
    org.opencontainers.image.source="https://github.com/talcum/ha-claude-code-addon" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.authors="David <david.talcum489@silomails.com>"
