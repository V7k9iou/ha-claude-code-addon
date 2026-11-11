# Crib Occupancy Detector

## How it works

Every `interval` seconds (default 5), for each camera:

1. Fetch one JPEG from `http://supervisor/core/api/camera_proxy/<entity_id>`
   with the add-on's Supervisor token. No camera credentials are needed.
2. Run the ONNX classifier on it (CPU, 2 threads) and get P(occupied).
3. Drop the image.
4. Compare with the threshold and feed a smoother:
   - **on** when the last `on_minutes` are fully covered by frames and at
     least 80% of them said occupied;
   - **off** when the last `off_minutes` are fully covered by frames and at
     most 20% of them said occupied;
   - otherwise the state holds, so a single misread frame never flips it.

After the add-on starts, the sensor is **unknown** (not off) until one of
those windows has filled: up to `on_minutes` with a child in the crib, or
`off_minutes` when it's empty. A restart therefore never reports a sleeping
child as gone. An automation that triggers on `to: "on"` will fire again
after a restart when the state goes from unknown to on. Add `from: "off"` if
that matters. After a camera outage the state holds until a full window has
been seen again.

A camera with no usable frame for 2 minutes goes unavailable. So does every
camera while there is no model yet, and every entity if the add-on stops or
crashes (MQTT last will).

The model check runs on its own, so downloading or loading a new model never
pauses detection.

## Options

```yaml
model_url: http://macmini.local:8765/model/latest.json
cameras:
  - entity_id: camera.nursery_medium_resolution_channel
    id: crib_detector            # entity ids: binary_sensor.crib_detector_occupied, ...
    name: Nursery crib           # device name in Home Assistant
    area: nursery                # optional: area for the device on first discovery
  - entity_id: camera.second_nursery_medium_resolution_channel
    id: crib_detector_2
    name: Second crib
interval: 5
on_minutes: 5
off_minutes: 2
model_check_interval: 3600
```

MQTT credentials come from the Mosquitto add-on unless `mqtt_host` is set.

## The model

`model_url` points at a JSON sidecar. The add-on checks it every
`model_check_interval` seconds (every minute while it has no model), downloads
the ONNX file it names when `version` changes, checks `sha256` if given,
loads it, and only then replaces the cached copy in `/data/model`. If the
training machine is off, the cached model keeps running.

```json
{
  "version": "2026-11-10.1",
  "model": "model.onnx",
  "sha256": "<hex of the onnx file>",
  "input": {"width": 224, "height": 224, "resize": "stretch",
            "color": "RGB", "layout": "NCHW", "scale": 255.0,
            "mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]},
  "output": {"type": "softmax", "occupied_index": 1},
  "threshold": 0.5,
  "cameras": {"crib_detector": {"threshold": 0.6}}
}
```

- `model`: relative to the sidecar URL, or absolute.
- `input.resize`: `stretch` (to width x height), `letterbox` (fit, pad with
  black) or `center_crop` (fill, crop the overflow). Applied to the whole
  snapshot, whatever its resolution.
- `input.color`: `RGB`, `BGR` or `L` (grayscale, 1 channel).
- `input.layout`: `NCHW` or `NHWC`. Input is float32, batch 1.
- Pixels are divided by `scale`, then `(x - mean) / std` per channel.
- `output.type`: `softmax` (logits; `occupied_index` picks the class),
  `sigmoid` (one logit) or `probability` (already P(occupied)).
- `threshold`: default for every camera; `cameras.<id>.threshold` overrides
  it per camera (keyed by the camera `id` option).

## Checking it

From a shell in the container: `/opt/venv/bin/python /opt/crib_detector/detector.py --check`
fetches the model and one frame per camera and prints numbers only (frame
size, probability, threshold, time taken).

Offline tests (no camera, broker or real model; onnxruntime is faked):
`python -m pytest crib_detector/tests` with numpy, pillow, aiohttp and pytest
installed.
