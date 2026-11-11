ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:9.5.2
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt

# Wheels only: every dependency has a prebuilt manylinux wheel, so no compiler
# is installed and pip fails loudly instead of trying to build one.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt /root/.cache

COPY rootfs /

RUN chmod a+x \
        /etc/services.d/crib_detector/run \
        /etc/services.d/crib_detector/finish

LABEL \
    org.opencontainers.image.title="Home Assistant Add-on: Crib Occupancy Detector" \
    org.opencontainers.image.description="Crib occupancy from camera snapshots and a local image classifier, via MQTT discovery." \
    org.opencontainers.image.source="https://github.com/talcum/ha-claude-code-addon" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.authors="David"
