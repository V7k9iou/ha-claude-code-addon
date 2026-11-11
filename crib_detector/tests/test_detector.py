"""Offline tests for the crib detector: no camera, broker, model or image data.

onnxruntime is replaced by a fake session (it has no wheel for every dev box),
so these cover everything around the model: smoothing, preprocessing, output
decoding, model download checks and the MQTT discovery payload. Frames are
synthetic solid-colour JPEGs generated here.

Run: python -m pytest crib_detector/tests
Needs: numpy, pillow, aiohttp, pytest.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


class FakeSession:
    """Stands in for ort.InferenceSession; returns ``FakeSession.output``."""

    output = np.array([[0.0, 0.0]], dtype=np.float32)
    last_input: np.ndarray | None = None

    def __init__(self, onnx_bytes, options=None, providers=None):
        if onnx_bytes == b"broken":
            raise RuntimeError("bad model")

    def get_inputs(self):
        return [types.SimpleNamespace(name="input")]

    def run(self, _names, feeds):
        FakeSession.last_input = feeds["input"]
        return [FakeSession.output]


fake_ort = types.ModuleType("onnxruntime")
fake_ort.InferenceSession = FakeSession
fake_ort.SessionOptions = lambda: types.SimpleNamespace()
sys.modules.setdefault("onnxruntime", fake_ort)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs/opt/crib_detector"))
import detector  # noqa: E402

detector.ort = fake_ort  # in case a real onnxruntime was imported first


def jpeg(color=(255, 0, 0), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def sidecar(**overrides) -> dict:
    base = {"version": "v1", "model": "model.onnx"}
    base.update(overrides)
    return base


# ----------------------------------------------------------------------------
# Smoother
# ----------------------------------------------------------------------------


def feed(smoother, start, seconds, occupied, step=5.0):
    """Frames every ``step`` s from ``start`` for ``seconds``; returns (end, states)."""
    states = []
    t = start
    while t < start + seconds:
        states.append(smoother.add(t, occupied))
        t += step
    return t, states


def make_smoother():
    return detector.Smoother(on_seconds=300, off_seconds=120, interval=5)


def test_starts_unknown_not_off():
    s = make_smoother()
    assert s.add(0, True) is None
    assert s.add(5, False) is None


def test_occupied_after_on_window():
    s = make_smoother()
    t, states = feed(s, 0, 290, True)
    assert states[-1] is None  # under 5 minutes: still undecided
    _, states = feed(s, t, 15, True)
    assert states[-1] is True


def test_empty_after_off_window_from_start():
    s = make_smoother()
    _, states = feed(s, 0, 110, False)
    assert states[-1] is None  # a single empty frame after a restart decides nothing
    _, states = feed(s, 110, 20, False)
    assert states[-1] is False


def test_one_misread_frame_does_not_flip():
    s = make_smoother()
    t, _ = feed(s, 0, 400, True)
    assert s.add(t, False) is True
    t, _ = feed(s, t + 5, 60, True)
    assert s.state is True
    # And the other way.
    s = make_smoother()
    t, _ = feed(s, 0, 200, False)
    assert s.state is False
    assert s.add(t, True) is False


def test_turns_off_after_off_window():
    s = make_smoother()
    t, _ = feed(s, 0, 400, True)
    t, states = feed(s, t, 90, False)
    assert states[-1] is True  # 90 s empty: still within the 2 min off window
    _, states = feed(s, t, 60, False)
    assert states[-1] is False


def test_outage_holds_state_until_window_refilled():
    s = make_smoother()
    t, _ = feed(s, 0, 400, True)
    assert s.state is True
    # Camera gone for 10 minutes, comes back showing an empty crib.
    t += 600
    assert s.add(t, False) is True  # one frame after the gap decides nothing
    _, states = feed(s, t + 5, 130, False)
    assert states[-1] is False


def test_gap_resets_on_coverage():
    s = make_smoother()
    t, _ = feed(s, 0, 150, True)
    t += 60  # a minute without frames inside the on window
    _, states = feed(s, t, 200, True)
    assert states[-1] is None  # not 5 continuous minutes yet
    feed(s, t + 200, 110, True)  # 310 s continuous since the gap
    assert s.state is True


# ----------------------------------------------------------------------------
# Model: preprocessing and output decoding
# ----------------------------------------------------------------------------


def test_preprocess_nchw_rgb_normalised():
    m = detector.Model(sidecar(input={"width": 32, "height": 16, "mean": [0.5] * 3, "std": [0.5] * 3}), b"x")
    x = m.preprocess(jpeg((255, 0, 0)))
    assert x.shape == (1, 3, 16, 32) and x.dtype == np.float32
    assert x[0, 0].mean() == pytest.approx(1.0, abs=0.05)  # red: (1 - .5) / .5
    assert x[0, 2].mean() == pytest.approx(-1.0, abs=0.05)  # blue: (0 - .5) / .5


def test_preprocess_bgr_nhwc():
    m = detector.Model(sidecar(input={"width": 8, "height": 8, "color": "BGR", "layout": "NHWC"}), b"x")
    x = m.preprocess(jpeg((255, 0, 0)))
    assert x.shape == (1, 8, 8, 3)
    assert x[0, ..., 2].mean() == pytest.approx(1.0, abs=0.03)  # red moved to the last channel
    assert x[0, ..., 0].mean() == pytest.approx(0.0, abs=0.03)


def test_preprocess_grayscale():
    m = detector.Model(sidecar(input={"width": 8, "height": 8, "color": "L"}), b"x")
    assert m.preprocess(jpeg()).shape == (1, 1, 8, 8)


@pytest.mark.parametrize("mode", ["stretch", "letterbox", "center_crop"])
def test_resize_modes_hit_target_size(mode):
    m = detector.Model(sidecar(input={"width": 20, "height": 20, "resize": mode}), b"x")
    for size in ((64, 48), (48, 64), (1280, 720)):
        assert m.preprocess(jpeg(size=size)).shape == (1, 3, 20, 20)


def test_letterbox_pads_black():
    m = detector.Model(sidecar(input={"width": 20, "height": 20, "resize": "letterbox"}), b"x")
    x = m.preprocess(jpeg((255, 255, 255), size=(80, 40)))
    assert x[0, 0, 0].mean() == pytest.approx(0.0, abs=0.01)  # top padding row
    assert x[0, 0, 10].mean() == pytest.approx(1.0, abs=0.03)  # image row


@pytest.mark.parametrize("output, raw, expected", [
    ({"type": "softmax", "occupied_index": 1}, [[0.0, 0.0]], 0.5),
    ({"type": "softmax", "occupied_index": 1}, [[0.0, 2.0]], 1 / (1 + np.exp(-2.0))),
    ({"type": "softmax", "occupied_index": 0}, [[2.0, 0.0]], 1 / (1 + np.exp(-2.0))),
    ({"type": "sigmoid"}, [[0.0]], 0.5),
    ({"type": "sigmoid"}, [[3.0]], 1 / (1 + np.exp(-3.0))),
    ({"type": "probability"}, [[0.83]], 0.83),
])
def test_output_decoding(output, raw, expected):
    FakeSession.output = np.array(raw, dtype=np.float32)
    m = detector.Model(sidecar(output=output), b"x")
    assert m.predict(jpeg()) == pytest.approx(expected, rel=1e-5)


def test_thresholds_per_camera():
    m = detector.Model(sidecar(threshold=0.4, cameras={"crib_detector": {"threshold": 0.7}}), b"x")
    assert m.threshold("crib_detector") == 0.7
    assert m.threshold("crib_detector_2") == 0.4


@pytest.mark.parametrize("bad", [
    {"input": {"resize": "zoom"}}, {"input": {"color": "CMYK"}},
    {"input": {"layout": "CHW"}}, {"output": {"type": "argmax"}},
])
def test_bad_sidecar_rejected(bad):
    with pytest.raises(detector.ModelError):
        detector.Model(sidecar(**bad), b"x")


def test_unloadable_model_rejected():
    with pytest.raises(detector.ModelError):
        detector.Model(sidecar(), b"broken")


# ----------------------------------------------------------------------------
# Model download
# ----------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def text(self):
        return self.body.decode()

    async def read(self):
        return self.body


class FakeHttp:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requested: list[str] = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        return FakeResponse(self.files[url])


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "MODEL_DIR", str(tmp_path / "model"))
    return tmp_path / "model"


def test_fetch_downloads_validates_and_caches(model_dir):
    onnx = b"model-bytes"
    side = sidecar(version="v2", sha256=hashlib.sha256(onnx).hexdigest())
    http = FakeHttp({"http://mac/m/latest.json": json.dumps(side).encode(), "http://mac/m/model.onnx": onnx})
    model = asyncio.run(detector.fetch_model(http, "http://mac/m/latest.json", "v1"))
    assert model.version == "v2"
    assert (model_dir / "model.onnx").read_bytes() == onnx
    assert detector.load_cached_model().version == "v2"


def test_fetch_same_version_skips_download(model_dir):
    http = FakeHttp({"http://mac/latest.json": json.dumps(sidecar(version="v1")).encode()})
    assert asyncio.run(detector.fetch_model(http, "http://mac/latest.json", "v1")) is None
    assert http.requested == ["http://mac/latest.json"]


def test_fetch_sha_mismatch_keeps_cache(model_dir):
    side = sidecar(version="v3", sha256="00" * 32)
    http = FakeHttp({"http://mac/latest.json": json.dumps(side).encode(), "http://mac/model.onnx": b"x"})
    with pytest.raises(detector.ModelError):
        asyncio.run(detector.fetch_model(http, "http://mac/latest.json", None))
    assert not model_dir.exists()


def test_fetch_unloadable_model_keeps_cache(model_dir):
    http = FakeHttp({"http://mac/latest.json": json.dumps(sidecar(version="v3")).encode(),
                     "http://mac/model.onnx": b"broken"})
    with pytest.raises(detector.ModelError):
        asyncio.run(detector.fetch_model(http, "http://mac/latest.json", None))
    assert not model_dir.exists()


# ----------------------------------------------------------------------------
# MQTT discovery
# ----------------------------------------------------------------------------


def test_discovery_payload():
    cfg = detector.Config.from_env()
    cam = detector.Camera(entity_id="camera.x", id="crib_detector", name="Crib", area="bedroom")
    payload = detector.discovery_payload(cfg, cam)
    assert payload["availability_mode"] == "all"
    assert [a["topic"] for a in payload["availability"]] == [
        detector.STATUS_TOPIC, "crib_detector/crib_detector/availability"]
    assert payload["device"]["suggested_area"] == "bedroom"
    occupied = payload["components"]["occupied"]
    assert occupied["default_entity_id"] == "binary_sensor.crib_detector_occupied"
    assert "is none" in occupied["value_template"]
    json.dumps(payload)  # must serialise


# ----------------------------------------------------------------------------
# One camera cycle, against a fake broker
# ----------------------------------------------------------------------------


class FakeMqtt:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


def test_first_frame_after_start_publishes_unknown_not_empty(model_dir):
    FakeSession.output = np.array([[5.0, -5.0]], dtype=np.float32)  # clearly "empty"
    cam = detector.Camera(entity_id="camera.x", id="crib_detector", name="Crib")
    cfg = detector.Config.from_env()
    cfg = detector.Config(**{**cfg.__dict__, "cameras": (cam,)})
    det = detector.Detector(cfg)
    det.model = detector.Model(sidecar(), b"x")
    det._mqtt = FakeMqtt()
    http = FakeHttp({"http://supervisor/core/api/camera_proxy/camera.x": jpeg()})
    http.get = lambda url, headers=None, timeout=None: FakeResponse(http.files[url])
    asyncio.run(det.run_camera(http, cam))
    state = [json.loads(p) for t, p, _ in det._mqtt.published if t.endswith("/state")]
    assert state == [{"occupied": None, "probability": 0.0, "model": "v1"}]
    assert ("crib_detector/crib_detector/availability", "online", True) in det._mqtt.published
