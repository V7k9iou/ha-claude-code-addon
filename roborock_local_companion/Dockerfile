ARG BUILD_FROM=ghcr.io/hassio-addons/base:20.1.0
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt

# Python runtime in a venv. Every compiled dependency (aiohttp, pycryptodome,
# Pillow via the map parser) ships musllinux wheels for Alpine, so the build
# deps are only a safety net and are removed again.
# hadolint ignore=DL3018
RUN apk add --no-cache python3 py3-pip \
    && apk add --no-cache --virtual .build-deps gcc musl-dev python3-dev \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && apk del .build-deps \
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
