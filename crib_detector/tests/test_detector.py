"""Offline tests for the crib detector: no camera, broker, model or image data.

onnxruntime is replaced by a fake session (it has no wheel for every dev box),
so these cover everything around the model: smoothing, the trainer's
preprocessing steps, sidecar validation, model download checks, telemetry and
the MQTT discovery payload. Frames are
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

    output = np.array([[0.5]], dtype=np.float32)
    last_input: np.ndarray | None = None

    def __init__(self, onnx_bytes, options=None, providers=None):
        if onnx_bytes == b"broken":
            raise RuntimeError("bad model")

    def get_inputs(self):
        return [types.SimpleNamespace(name="image")]

    def get_outputs(self):
        return [types.SimpleNamespace(name="p_occupied")]

    def run(self, names, feeds):
        assert names == ["p_occupied"]
        FakeSession.last_input = feeds["image"]
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


SIDECAR_CAMERAS = {
    "thomas": {"crop": [0.059961, 0.017889, 0.950195, 0.907111], "threshold": 0.580204},
    "sophie": {"crop": [0.062562, 0.040222, 0.943688, 0.876444], "threshold": 0.529667},
}


def sidecar(**overrides) -> dict:
    """The trainer's sidecar shape, with a small input size to keep tests fast."""
    base = {
        "version": "v1",
        "onnx": {"sha256": "unused"},
        "input": {"name": "image", "shape": [1, 3, 32, 32], "layout": "NCHW", "color": "RGB", "range": [0, 255]},
        "output": {"name": "p_occupied", "shape": [1, 1]},
        "cameras": SIDECAR_CAMERAS,
    }
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
# Model: the trainer's sidecar contract
# ----------------------------------------------------------------------------


def test_crop_box_matches_trainer_formula():
    # 640x360 snapshot with Thomas' published crop.
    assert detector.Model.crop_box((0.059961, 0.017889, 0.950195, 0.907111), 640, 360) == (38, 6, 608, 327)
    # Degenerate and out-of-range boxes are clamped, never empty.
    assert detector.Model.crop_box((0.5, 0.5, 0.5, 0.5), 10, 10) == (5, 5, 6, 6)
    assert detector.Model.crop_box((0.0, 0.0, 1.0, 1.0), 10, 10) == (0, 0, 10, 10)
    assert detector.Model.crop_box((0.999, 0.999, 1.0, 1.0), 10, 10) == (9, 9, 10, 10)


def test_preprocess_raw_pixels_nchw():
    m = detector.Model(sidecar(), b"x")
    x = m.preprocess(jpeg((255, 0, 0), size=(640, 360)), "thomas")
    assert x.shape == (1, 3, 32, 32) and x.dtype == np.float32
    assert x[0, 0].mean() == pytest.approx(254, abs=3)  # raw 0..255, no normalisation
    assert x[0, 2].mean() == pytest.approx(0, abs=3)


def test_preprocess_is_exactly_the_trainer_steps():
    m = detector.Model(sidecar(), b"x")
    rng = np.random.default_rng(1)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 256, (360, 640, 3), dtype=np.uint8)).save(buf, "JPEG")
    data = buf.getvalue()
    # The steps as published by the trainer, written out independently.
    img = Image.open(io.BytesIO(data)).convert("RGB")
    W, H = img.size
    x0, y0, x1, y1 = SIDECAR_CAMERAS["thomas"]["crop"]
    left = min(max(int(round(x0 * W)), 0), W - 1)
    top = min(max(int(round(y0 * H)), 0), H - 1)
    right = min(max(int(round(x1 * W)), left + 1), W)
    bottom = min(max(int(round(y1 * H)), top + 1), H)
    ref = img.crop((left, top, right, bottom)).resize((32, 32), Image.Resampling.BILINEAR, reducing_gap=None)
    ref = np.asarray(ref, dtype=np.uint8).transpose(2, 0, 1).astype(np.float32)[None]
    np.testing.assert_array_equal(m.preprocess(data, "thomas"), ref)


def test_predict_feeds_named_input_and_reads_probability():
    FakeSession.output = np.array([[0.83]], dtype=np.float32)
    m = detector.Model(sidecar(), b"x")
    assert m.predict(jpeg(), "sophie") == pytest.approx(0.83, rel=1e-6)
    assert FakeSession.last_input.shape == (1, 3, 32, 32)


def test_thresholds_per_camera_key():
    m = detector.Model(sidecar(), b"x")
    assert m.threshold("thomas") == 0.580204
    assert m.threshold("sophie") == 0.529667


def test_missing_camera_key_is_an_error():
    with pytest.raises(detector.ModelError, match="no camera entry"):
        detector.Model(sidecar(), b"x", required_keys=("thomas", "crib_detector_2"))
    detector.Model(sidecar(), b"x", required_keys=("thomas", "sophie"))  # fine


@pytest.mark.parametrize("change", [
    {"input": {"name": "image", "shape": [1, 3, 32, 32], "layout": "NHWC"}},
    {"input": {"name": "image", "shape": [1, 3, 32, 32], "color": "BGR"}},
    {"input": {"name": "image", "shape": [1, 3, 32, 32], "range": [0, 1]}},
    {"input": {"name": "image", "shape": [1, 3, 32, 16]}},
    {"input": {"name": "image", "shape": [1, 1, 32, 32]}},
    {"input": {"name": "other", "shape": [1, 3, 32, 32]}},
    {"output": {"name": "p_occupied", "shape": [1, 2]}},
    {"output": {"name": "logits", "shape": [1, 1]}},
    {"cameras": {"thomas": {"crop": [0.5, 0, 0.4, 1], "threshold": 0.5}}},
    {"cameras": {"thomas": {"crop": [0, 0, 1, 1], "threshold": 1.5}}},
    {"cameras": {"thomas": {"threshold": 0.5}}},
    {"input": None},
])
def test_unsupported_or_malformed_sidecar_rejected(change):
    with pytest.raises(detector.ModelError):
        detector.Model(sidecar(**change), b"x")


def test_unloadable_model_rejected():
    with pytest.raises(detector.ModelError):
        detector.Model(sidecar(), b"broken")


# ----------------------------------------------------------------------------
# Model download
# ----------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise detector.aiohttp.ClientResponseError(None, (), status=self.status)

    async def text(self):
        return self.body.decode()

    async def read(self):
        return self.body


class FakeHttp:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requested: list[tuple[str, dict | None]] = []
        self.posted: list = []
        self.post_status = 200

    def get(self, url, headers=None, timeout=None):
        self.requested.append((url, headers))
        return FakeResponse(self.files[url])

    def post(self, url, json=None, headers=None, timeout=None):
        if self.post_status == 200:
            self.posted.append((url, json, headers))
        return FakeResponse(b"", self.post_status)


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "MODEL_DIR", str(tmp_path / "model"))
    return tmp_path / "model"


SIDECAR_URL = "http://mac:8091/model/latest.json"
AUTH = {"Authorization": "Bearer t0k"}


def serve(version="2026-11-11.1", onnx=b"model-bytes", sha=None):
    side = sidecar(version=version, onnx={"sha256": sha or hashlib.sha256(onnx).hexdigest()})
    return FakeHttp({SIDECAR_URL: json.dumps(side).encode(),
                     f"http://mac:8091/model/{version}.onnx": onnx})


def test_fetch_builds_onnx_url_from_version_with_auth_and_caches(model_dir):
    http = serve()
    model = asyncio.run(detector.fetch_model(http, SIDECAR_URL, "old", AUTH, ("thomas", "sophie")))
    assert model.version == "2026-11-11.1"
    assert http.requested == [(SIDECAR_URL, AUTH), ("http://mac:8091/model/2026-11-11.1.onnx", AUTH)]
    assert (model_dir / "model.onnx").read_bytes() == b"model-bytes"
    assert detector.load_cached_model(("thomas", "sophie")).version == "2026-11-11.1"
    assert detector.load_cached_model(("thomas", "nobody")) is None  # key check applies to the cache too


def test_fetch_same_version_skips_download(model_dir):
    http = serve(version="v1")
    assert asyncio.run(detector.fetch_model(http, SIDECAR_URL, "v1", AUTH)) is None
    assert [u for u, _ in http.requested] == [SIDECAR_URL]


def test_fetch_sha_mismatch_keeps_cache(model_dir):
    http = serve(sha="00" * 32)
    with pytest.raises(detector.ModelError, match="sha256"):
        asyncio.run(detector.fetch_model(http, SIDECAR_URL, None, AUTH))
    assert not model_dir.exists()


def test_fetch_without_sha_is_refused(model_dir):
    side = sidecar(version="v2", onnx={})
    http = FakeHttp({SIDECAR_URL: json.dumps(side).encode()})
    with pytest.raises(detector.ModelError, match="onnx.sha256"):
        asyncio.run(detector.fetch_model(http, SIDECAR_URL, None, AUTH))


def test_fetch_missing_camera_key_keeps_cache(model_dir):
    with pytest.raises(detector.ModelError, match="no camera entry"):
        asyncio.run(detector.fetch_model(serve(), SIDECAR_URL, None, AUTH, ("thomas", "crib_detector_2")))
    assert not model_dir.exists()


def test_fetch_unloadable_model_keeps_cache(model_dir):
    with pytest.raises(detector.ModelError):
        asyncio.run(detector.fetch_model(serve(onnx=b"broken"), SIDECAR_URL, None, AUTH))
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


def make_detector(telemetry=True):
    cam = detector.Camera(entity_id="camera.x", id="crib_detector", name="Crib", model_key="thomas")
    cfg = detector.Config.from_env()
    cfg = detector.Config(**{**cfg.__dict__, "cameras": (cam,), "model_url": SIDECAR_URL,
                             "model_token": "t0k", "telemetry": telemetry})
    det = detector.Detector(cfg)
    det.model = detector.Model(sidecar(), b"x", cfg.model_keys)
    det._mqtt = FakeMqtt()
    http = FakeHttp({"http://supervisor/core/api/camera_proxy/camera.x": jpeg()})
    return det, cam, http


def test_first_frame_after_start_publishes_unknown_not_empty(model_dir):
    FakeSession.output = np.array([[0.01]], dtype=np.float32)  # clearly "empty"
    det, cam, http = make_detector()
    asyncio.run(det.run_camera(http, cam))
    state = [json.loads(p) for t, p, _ in det._mqtt.published if t.endswith("/state")]
    assert state == [{"occupied": None, "probability": 1.0, "model": "v1"}]
    assert ("crib_detector/crib_detector/availability", "online", True) in det._mqtt.published


def test_telemetry_batches_numbers_under_the_model_key(model_dir):
    FakeSession.output = np.array([[0.75]], dtype=np.float32)
    det, cam, http = make_detector()
    assert det.cfg.telemetry_url == "http://mac:8091/telemetry"
    asyncio.run(det.run_camera(http, cam))
    asyncio.run(det.run_camera(http, cam))
    http.post_status = 503
    asyncio.run(det.send_telemetry(http))  # trainer down: readings kept
    assert len(det._telemetry) == 2 and http.posted == []
    http.post_status = 200
    asyncio.run(det.send_telemetry(http))
    (url, batch, headers), = http.posted
    assert url == "http://mac:8091/telemetry" and headers == AUTH
    assert [set(b) for b in batch] == [{"camera", "ts", "p_occupied", "model_version"}] * 2
    assert batch[0]["camera"] == "thomas" and batch[0]["p_occupied"] == 0.75
    assert isinstance(batch[0]["ts"], int) and batch[0]["model_version"] == "v1"
    assert not det._telemetry


def test_telemetry_off_collects_nothing(model_dir):
    det, cam, http = make_detector(telemetry=False)
    assert det.cfg.telemetry_url == ""
    asyncio.run(det.run_camera(http, cam))
    assert not det._telemetry
