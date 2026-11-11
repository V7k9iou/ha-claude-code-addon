#!/opt/venv/bin/python
"""Crib Occupancy Detector.

Every few seconds, for each configured camera: fetch one JPEG snapshot through
Home Assistant's camera proxy, run an image classifier (ONNX, trained
elsewhere) that answers "is a child in the crib", and publish the result to
Home Assistant over MQTT discovery.

PRIVACY: snapshots exist only in memory, for one inference, and are then
dropped. Nothing here writes, logs or publishes image data -- only the
probability and the derived on/off state leave this process.

Entities per camera (one MQTT device each):
  binary_sensor <id>_occupied     -- on once the child has been in the crib for
                                     on_minutes (default 5), off once the crib
                                     has looked empty for off_minutes; unknown
                                     after a start until one of those is met
  sensor        <id>_probability  -- latest raw P(occupied), % (diagnostic)
  sensor        <id>_model        -- model version in use (diagnostic)

The model is pulled from ``model_url``: a JSON sidecar (format below) that
names the ONNX file next to it. Both are cached in /data so the detector keeps
working when the training machine is off.

Sidecar (all keys except "version" and "model" optional)::

    {
      "version": "2026-11-10.1",
      "model": "model.onnx",              # relative to the sidecar URL
      "sha256": "<hex of the onnx file>",
      "input": {"width": 224, "height": 224,
                "resize": "stretch",      # stretch | letterbox | center_crop
                "color": "RGB",           # RGB | BGR | L (grayscale)
                "layout": "NCHW",         # NCHW | NHWC
                "scale": 255.0,           # pixel / scale, then (x - mean) / std
                "mean": [0.485, 0.456, 0.406],
                "std": [0.229, 0.224, 0.225]},
      "output": {"type": "softmax",       # softmax | sigmoid | probability
                 "occupied_index": 1},    # which output is "occupied" (softmax)
      "threshold": 0.5,
      "cameras": {"<camera id>": {"threshold": 0.6}}
    }
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin

import aiohttp
import numpy as np
import onnxruntime as ort
from PIL import Image

_LOGGER = logging.getLogger("crib_detector")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MODEL_DIR = os.path.join(DATA_DIR, "model")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
MODEL_TIMEOUT = aiohttp.ClientTimeout(total=300)
MQTT_RECONNECT = 10.0
# Add-on level availability: "online" once connected, "offline" via the MQTT
# last will if the process dies, so HA never shows a frozen state as live.
STATUS_TOPIC = "crib_detector/status"
# Retry interval for the model URL while there is no model at all.
NO_MODEL_RETRY = 60.0
# A camera with no good frame for this long goes unavailable in HA.
STALE_AFTER = 120.0
# Hysteresis on the smoothed state: on needs this share of "occupied" frames
# over the whole on-window, off needs at most OFF_FRACTION over the off-window.
# One misread frame can't flip either way.
ON_FRACTION = 0.8
OFF_FRACTION = 0.2


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Camera:
    entity_id: str  # camera.xxx in Home Assistant
    id: str  # entity id slug for our entities, e.g. crib_detector
    name: str  # device name in HA, e.g. "Thomas' room crib"
    area: str = ""


@dataclass(frozen=True)
class Config:
    cameras: tuple[Camera, ...]
    model_url: str
    model_check_interval: float
    interval: float
    on_minutes: float
    off_minutes: float
    ha_url: str
    ha_token: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    discovery_prefix: str
    addon_version: str

    @classmethod
    def from_env(cls) -> Config:
        cameras = tuple(
            Camera(
                entity_id=c["entity_id"],
                id=c["id"],
                name=c.get("name") or c["id"],
                area=c.get("area") or "",
            )
            for c in json.loads(os.environ.get("CAMERAS", "[]"))
        )
        return cls(
            cameras=cameras,
            model_url=os.environ.get("MODEL_URL", "").strip(),
            model_check_interval=float(os.environ.get("MODEL_CHECK_INTERVAL", "3600")),
            interval=float(os.environ.get("INTERVAL", "5")),
            on_minutes=float(os.environ.get("ON_MINUTES", "5")),
            off_minutes=float(os.environ.get("OFF_MINUTES", "2")),
            ha_url=os.environ.get("HA_URL", "http://supervisor/core").rstrip("/"),
            ha_token=os.environ.get("SUPERVISOR_TOKEN", ""),
            mqtt_host=os.environ.get("MQTT_HOST", ""),
            mqtt_port=int(os.environ.get("MQTT_PORT", "1883") or 1883),
            mqtt_username=os.environ.get("MQTT_USERNAME", ""),
            mqtt_password=os.environ.get("MQTT_PASSWORD", ""),
            discovery_prefix=os.environ.get("DISCOVERY_PREFIX", "homeassistant"),
            addon_version=os.environ.get("ADDON_VERSION", "dev"),
        )


# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------


class ModelError(Exception):
    """The sidecar or the ONNX file is unusable."""


class Model:
    """One ONNX classifier plus the sidecar that says how to feed it."""

    def __init__(self, sidecar: dict, onnx_bytes: bytes) -> None:
        self.sidecar = sidecar
        self.version = str(sidecar.get("version") or "unknown")
        spec = sidecar.get("input") or {}
        self.width = int(spec.get("width", 224))
        self.height = int(spec.get("height", 224))
        self.resize = str(spec.get("resize", "stretch")).lower()
        self.color = str(spec.get("color", "RGB")).upper()
        self.layout = str(spec.get("layout", "NCHW")).upper()
        self.scale = float(spec.get("scale", 255.0))
        channels = 1 if self.color == "L" else 3
        self.mean = np.asarray(spec.get("mean", [0.0] * channels), dtype=np.float32)
        self.std = np.asarray(spec.get("std", [1.0] * channels), dtype=np.float32)
        out = sidecar.get("output") or {}
        self.output_type = str(out.get("type", "softmax")).lower()
        self.occupied_index = int(out.get("occupied_index", 1))
        self.default_threshold = float(sidecar.get("threshold", 0.5))
        self.thresholds = {
            key: float(value["threshold"])
            for key, value in (sidecar.get("cameras") or {}).items()
            if isinstance(value, dict) and "threshold" in value
        }
        if self.resize not in ("stretch", "letterbox", "center_crop"):
            raise ModelError(f"unknown resize {self.resize!r}")
        if self.color not in ("RGB", "BGR", "L"):
            raise ModelError(f"unknown color {self.color!r}")
        if self.layout not in ("NCHW", "NHWC"):
            raise ModelError(f"unknown layout {self.layout!r}")
        if self.output_type not in ("softmax", "sigmoid", "probability"):
            raise ModelError(f"unknown output type {self.output_type!r}")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2  # leave the Pi's other cores to HA
        try:
            self.session = ort.InferenceSession(onnx_bytes, options, providers=["CPUExecutionProvider"])
        except Exception as err:  # noqa: BLE001 - onnxruntime raises its own types
            raise ModelError(f"onnxruntime could not load the model: {err}") from err
        self.input_name = self.session.get_inputs()[0].name

    def threshold(self, camera_id: str) -> float:
        return self.thresholds.get(camera_id, self.default_threshold)

    def preprocess(self, jpeg: bytes) -> np.ndarray:
        with Image.open(io.BytesIO(jpeg)) as img:
            img = img.convert("L" if self.color == "L" else "RGB")
            img = self._resize(img)
            pixels = np.asarray(img, dtype=np.float32)
        if pixels.ndim == 2:
            pixels = pixels[:, :, None]
        if self.color == "BGR":
            pixels = pixels[:, :, ::-1]
        pixels = (pixels / self.scale - self.mean) / self.std
        if self.layout == "NCHW":
            pixels = pixels.transpose(2, 0, 1)
        return np.ascontiguousarray(pixels[None, ...], dtype=np.float32)

    def _resize(self, img: Image.Image) -> Image.Image:
        size = (self.width, self.height)
        if self.resize == "stretch":
            return img.resize(size, Image.BILINEAR)
        if self.resize == "center_crop":
            ratio = max(self.width / img.width, self.height / img.height)
            scaled = img.resize((round(img.width * ratio), round(img.height * ratio)), Image.BILINEAR)
            left = (scaled.width - self.width) // 2
            top = (scaled.height - self.height) // 2
            return scaled.crop((left, top, left + self.width, top + self.height))
        ratio = min(self.width / img.width, self.height / img.height)
        scaled = img.resize((round(img.width * ratio), round(img.height * ratio)), Image.BILINEAR)
        canvas = Image.new(img.mode, size)  # black, like the privacy mask
        canvas.paste(scaled, ((self.width - scaled.width) // 2, (self.height - scaled.height) // 2))
        return canvas

    def predict(self, jpeg: bytes) -> float:
        """P(occupied) for one snapshot."""
        raw = self.session.run(None, {self.input_name: self.preprocess(jpeg)})[0]
        values = np.asarray(raw, dtype=np.float64).reshape(-1)
        if self.output_type == "probability":
            return float(values[0])
        if self.output_type == "sigmoid":
            return float(1.0 / (1.0 + np.exp(-values[0])))
        exp = np.exp(values - values.max())
        return float(exp[self.occupied_index] / exp.sum())


def load_cached_model() -> Model | None:
    try:
        with open(os.path.join(MODEL_DIR, "sidecar.json"), encoding="utf-8") as fh:
            sidecar = json.load(fh)
        with open(os.path.join(MODEL_DIR, "model.onnx"), "rb") as fh:
            onnx_bytes = fh.read()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        _LOGGER.warning("Ignoring unreadable cached model: %s", err)
        return None
    try:
        return Model(sidecar, onnx_bytes)
    except ModelError as err:
        _LOGGER.warning("Ignoring cached model: %s", err)
        return None


def save_cached_model(sidecar: dict, onnx_bytes: bytes) -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)
    for name, data in (("model.onnx", onnx_bytes), ("sidecar.json", json.dumps(sidecar).encode())):
        tmp = os.path.join(MODEL_DIR, name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(MODEL_DIR, name))


async def fetch_model(http: aiohttp.ClientSession, url: str, current: str | None) -> Model | None:
    """The model at ``url`` if its version differs from ``current``, else None."""
    async with http.get(url, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        sidecar = json.loads(await resp.text())
    if not isinstance(sidecar, dict) or not sidecar.get("model") or not sidecar.get("version"):
        raise ModelError("sidecar needs at least 'version' and 'model'")
    if str(sidecar["version"]) == current:
        return None
    model_url = urljoin(url, str(sidecar["model"]))
    async with http.get(model_url, timeout=MODEL_TIMEOUT) as resp:
        resp.raise_for_status()
        onnx_bytes = await resp.read()
    expected = sidecar.get("sha256")
    if expected and hashlib.sha256(onnx_bytes).hexdigest() != str(expected).lower():
        raise ModelError(f"sha256 mismatch for {model_url}")
    # Validates before it replaces the cache. Both in a thread: building the
    # session takes seconds on the Pi and must not stall the camera loop.
    model = await asyncio.to_thread(Model, sidecar, onnx_bytes)
    await asyncio.to_thread(save_cached_model, sidecar, onnx_bytes)
    return model


# ----------------------------------------------------------------------------
# Smoothing
# ----------------------------------------------------------------------------


@dataclass
class Smoother:
    """Turns per-frame yes/no into a stable occupied state.

    On: the last ``on_seconds`` are fully covered by frames and at least
    ON_FRACTION of them said occupied. Off: the last ``off_seconds`` are fully
    covered and at most OFF_FRACTION of them said occupied. In between, the
    state holds. It starts as None (unknown), so a restart never reports a
    sleeping child as "empty"; and after a camera outage the state holds until
    a full window has been seen again.
    """

    on_seconds: float
    off_seconds: float
    interval: float
    samples: deque = field(default_factory=deque)  # (monotonic time, occupied)
    state: bool | None = None

    def add(self, now: float, occupied: bool) -> bool | None:
        self.samples.append((now, occupied))
        horizon = max(self.on_seconds, self.off_seconds) + 2 * self.interval
        while self.samples and now - self.samples[0][0] > horizon:
            self.samples.popleft()
        if self.state is not True and self._covered(now, self.on_seconds) \
                and self._fraction(now, self.on_seconds) >= ON_FRACTION:
            self.state = True
        elif self.state is not False and self._covered(now, self.off_seconds) \
                and self._fraction(now, self.off_seconds) <= OFF_FRACTION:
            self.state = False
        return self.state

    def _window(self, now: float, seconds: float) -> list[bool]:
        return [occ for t, occ in self.samples if now - t <= seconds]

    def _covered(self, now: float, seconds: float) -> bool:
        # The oldest frame reaches back (nearly) the whole window, and frames
        # did not stop arriving for long in between (a camera outage resets it).
        window = [t for t, _ in self.samples if now - t <= seconds + self.interval]
        if not window or now - window[0] < seconds - self.interval:
            return False
        gaps = (b - a for a, b in zip(window, window[1:]))
        return max(gaps, default=0.0) <= max(4 * self.interval, 30.0)

    def _fraction(self, now: float, seconds: float) -> float:
        window = self._window(now, seconds)
        return sum(window) / len(window) if window else 0.0


# ----------------------------------------------------------------------------
# MQTT payloads
# ----------------------------------------------------------------------------


def base_topic(camera: Camera) -> str:
    return f"crib_detector/{camera.id}"


def discovery_topic(cfg: Config, camera: Camera) -> str:
    return f"{cfg.discovery_prefix}/device/crib_detector_{camera.id}/config"


def discovery_payload(cfg: Config, camera: Camera) -> dict:
    uid = f"crib_detector_{camera.id}"
    topic = base_topic(camera)
    device = {
        "identifiers": [uid],
        "name": camera.name,
        "manufacturer": "Crib Occupancy Detector",
        "model": "Image classifier",
        "sw_version": cfg.addon_version,
    }
    if camera.area:
        device["suggested_area"] = camera.area
    return {
        "device": device,
        "origin": {"name": "Crib Occupancy Detector", "sw_version": cfg.addon_version},
        "state_topic": f"{topic}/state",
        # Both must be online: the add-on (last will) and this camera's frames.
        "availability": [{"topic": STATUS_TOPIC}, {"topic": f"{topic}/availability"}],
        "availability_mode": "all",
        "qos": 0,
        "components": {
            "occupied": {
                "platform": "binary_sensor",
                "name": "Occupied",
                "unique_id": f"{uid}_occupied",
                "default_entity_id": f"binary_sensor.{camera.id}_occupied",
                "device_class": "occupancy",
                # null while the smoother is undecided -> "None" -> unknown in HA
                "value_template": "{{ None if value_json.occupied is none "
                                  "else ('ON' if value_json.occupied else 'OFF') }}",
            },
            "probability": {
                "platform": "sensor",
                "name": "Occupied probability",
                "unique_id": f"{uid}_probability",
                "default_entity_id": f"sensor.{camera.id}_probability",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "suggested_display_precision": 0,
                "value_template": "{{ value_json.probability }}",
            },
            "model": {
                "platform": "sensor",
                "name": "Model version",
                "unique_id": f"{uid}_model",
                "default_entity_id": f"sensor.{camera.id}_model",
                "entity_category": "diagnostic",
                "icon": "mdi:brain",
                "value_template": "{{ value_json.model }}",
            },
        },
    }


# ----------------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------------


class Detector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.model: Model | None = load_cached_model()
        if self.model:
            _LOGGER.info("Loaded cached model %s", self.model.version)
        self.smoothers = {
            c.id: Smoother(cfg.on_minutes * 60, cfg.off_minutes * 60, cfg.interval) for c in cfg.cameras
        }
        self.last_ok = {c.id: 0.0 for c in cfg.cameras}
        self.available = {c.id: None for c in cfg.cameras}
        self._mqtt = None

    async def _publish(self, topic: str, payload, retain: bool = False) -> None:
        if self._mqtt is None:
            return
        data = payload if isinstance(payload, str) else json.dumps(payload)
        await self._mqtt.publish(topic, data, qos=0, retain=retain)

    async def _set_available(self, camera: Camera, available: bool) -> None:
        if self.available[camera.id] != available:
            await self._publish(f"{base_topic(camera)}/availability",
                                "online" if available else "offline", retain=True)
            self.available[camera.id] = available
            _LOGGER.info("%s %s", camera.name, "available" if available else "unavailable")

    async def snapshot(self, http: aiohttp.ClientSession, camera: Camera) -> bytes:
        url = f"{self.cfg.ha_url}/api/camera_proxy/{camera.entity_id}"
        async with http.get(url, headers={"Authorization": f"Bearer {self.cfg.ha_token}"},
                            timeout=HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def check_model(self, http: aiohttp.ClientSession) -> None:
        if not self.cfg.model_url:
            return
        try:
            new = await fetch_model(http, self.cfg.model_url, self.model.version if self.model else None)
        except (aiohttp.ClientError, TimeoutError, ValueError, OSError, ModelError) as err:
            _LOGGER.warning("Model check failed (%s); %s", err,
                            f"keeping {self.model.version}" if self.model else "no model yet")
            return
        if new:
            _LOGGER.info("Model %s -> %s", self.model.version if self.model else "none", new.version)
            self.model = new

    async def run_camera(self, http: aiohttp.ClientSession, camera: Camera) -> None:
        now = time.monotonic()
        model = self.model
        if model is None:
            await self._set_available(camera, False)
            return
        try:
            jpeg = await self.snapshot(http, camera)
            probability = await asyncio.to_thread(model.predict, jpeg)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("%s: snapshot failed: %s", camera.name, err)
            probability = None
        except Exception as err:  # noqa: BLE001 - a bad frame must not stop the loop
            _LOGGER.warning("%s: inference failed: %s", camera.name, type(err).__name__)
            probability = None
        finally:
            jpeg = None  # noqa: F841 - drop the image reference right away
        if probability is None:
            if now - self.last_ok[camera.id] > STALE_AFTER:
                await self._set_available(camera, False)
            return
        self.last_ok[camera.id] = now
        occupied = self.smoothers[camera.id].add(now, probability >= model.threshold(camera.id))
        await self._publish(f"{base_topic(camera)}/state", {
            "occupied": occupied,
            "probability": round(probability * 100, 1),
            "model": model.version,
        }, retain=True)
        await self._set_available(camera, True)

    async def model_loop(self, http: aiohttp.ClientSession) -> None:
        """Own task, so a slow download never pauses the cameras."""
        while True:
            await self.check_model(http)
            await asyncio.sleep(self.cfg.model_check_interval if self.model else NO_MODEL_RETRY)

    async def loop(self, http: aiohttp.ClientSession) -> None:
        while True:
            started = time.monotonic()
            await asyncio.gather(*(self.run_camera(http, c) for c in self.cfg.cameras))
            await asyncio.sleep(max(0.5, self.cfg.interval - (time.monotonic() - started)))

    async def run(self) -> None:
        import aiomqtt

        async with aiohttp.ClientSession() as http:
            model_task = asyncio.create_task(self.model_loop(http))
            try:
                await self._run_mqtt(http, aiomqtt)
            finally:
                model_task.cancel()

    async def _run_mqtt(self, http: aiohttp.ClientSession, aiomqtt) -> None:
        while True:
            try:
                async with aiomqtt.Client(
                    self.cfg.mqtt_host, self.cfg.mqtt_port,
                    username=self.cfg.mqtt_username or None,
                    password=self.cfg.mqtt_password or None,
                    identifier="crib_detector",
                    keepalive=60,
                    will=aiomqtt.Will(STATUS_TOPIC, "offline", qos=1, retain=True),
                ) as mqtt:
                    _LOGGER.info("Connected to MQTT broker %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
                    self._mqtt = mqtt
                    self.available = {c.id: None for c in self.cfg.cameras}
                    for camera in self.cfg.cameras:
                        await self._publish(discovery_topic(self.cfg, camera),
                                            discovery_payload(self.cfg, camera), retain=True)
                    await self._publish(STATUS_TOPIC, "online", retain=True)
                    await self.loop(http)
            except aiomqtt.MqttError as err:
                _LOGGER.warning("MQTT connection lost: %s (retry in %.0fs)", err, MQTT_RECONNECT)
            finally:
                self._mqtt = None
            await asyncio.sleep(MQTT_RECONNECT)

    async def check(self) -> None:
        """--check: fetch the model and one frame per camera, print numbers only."""
        async with aiohttp.ClientSession() as http:
            await self.check_model(http)
            print("model:", self.model.version if self.model else None)
            for camera in self.cfg.cameras:
                started = time.monotonic()
                jpeg = await self.snapshot(http, camera)
                size = len(jpeg)
                p = await asyncio.to_thread(self.model.predict, jpeg) if self.model else None
                jpeg = None
                print(f"{camera.id}: {size} bytes, P(occupied)={p}, "
                      f"threshold={self.model.threshold(camera.id) if self.model else None}, "
                      f"{time.monotonic() - started:.2f}s")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    detector = Detector(cfg)
    if "--check" in sys.argv:
        asyncio.run(detector.check())
        return
    if not cfg.cameras:
        sys.exit("No cameras configured")
    if not cfg.mqtt_host:
        sys.exit("MQTT_HOST is required")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(detector.run())


if __name__ == "__main__":
    main()
