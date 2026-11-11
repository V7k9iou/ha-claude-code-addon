# Crib Occupancy Detector

## How it works

Every `interval` seconds (default 5), for each camera:

1. Fetch one JPEG from `http://supervisor/core/api/camera_proxy/<entity_id>`
   with the add-on's Supervisor token. No camera credentials are needed.
2. Crop it to that camera's crib box, stretch to the model's input size, and
   run the ONNX classifier (CPU, 2 threads) to get P(occupied).
3. Drop the image.
4. Compare with that camera's threshold and feed a smoother:
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
model_url: http://<trainer>:<port>/model/latest.json
model_token: <bearer token for the model server>
telemetry: true                  # POST P(occupied) back to the trainer, numbers only
cameras:
  - entity_id: camera.<first>_medium_resolution_channel
    id: crib_detector            # entity ids: binary_sensor.crib_detector_occupied, ...
    name: First crib             # device name in Home Assistant
    area: first_room             # optional: area for the device on first discovery
    model_key: first             # this camera's entry in the sidecar (default: id)
  - entity_id: camera.<second>_medium_resolution_channel
    id: crib_detector_2
    name: Second crib
    model_key: second
interval: 5
on_minutes: 5
off_minutes: 2
model_check_interval: 3600
```

Match cameras to rooms by device identity (the camera's MAC under the device's
connections in Home Assistant), not by entity ID or area: Protect entity IDs
can carry old names.

MQTT credentials come from the Mosquitto add-on unless `mqtt_host` is set.

## The model

The model is trained elsewhere and served on the LAN. The add-on checks
`model_url` (a JSON sidecar) every `model_check_interval` seconds, or every
minute while it has no model. When `version` changes it downloads
`<version>.onnx` from the same directory, checks it against `onnx.sha256`,
loads it, and only then replaces the cached copy in `/data/model`. If the
trainer is off, the cached model keeps running. `model_token` is sent as
`Authorization: Bearer` on every request.

```json
{
  "version": "2026-11-11.1",
  "onnx": {"sha256": "<hex of 2026-11-11.1.onnx>"},
  "input": {"name": "image", "shape": [1, 3, 320, 320], "layout": "NCHW",
            "color": "RGB", "range": [0, 255]},
  "output": {"name": "p_occupied", "shape": [1, 1]},
  "cameras": {
    "first":  {"crop": [0.06, 0.02, 0.95, 0.91], "threshold": 0.58},
    "second": {"crop": [0.06, 0.04, 0.94, 0.88], "threshold": 0.53}
  }
}
```

Per camera, exactly as the trainer prepared its data:

1. Decode the JPEG to RGB (W x H).
2. Crop to `crop` (fractions of W and H, rounded and clamped so the box is
   never empty).
3. Stretch to S x S (`input.shape`), Pillow bilinear, `reducing_gap=None`.
4. Feed raw 0..255 as float32, NCHW, batch 1. Normalisation is inside the
   graph.
5. The output is already P(occupied). Occupied when it's at least that
   camera's `threshold`.

The add-on only implements this format. A sidecar that asks for anything
else, such as another layout, colour order or value range, is rejected and
the cached model keeps running. So is a sidecar with no entry for a
configured camera's `model_key`: a missing camera is an error, never a
default.

With `telemetry` on, each reading goes back to the trainer once a minute as
`POST /telemetry` with a list of `{camera: <model_key>, ts, p_occupied,
model_version}`. If the trainer is down, readings are kept (about 4 hours'
worth) and sent later.

## Checking it

From a shell in the container: `/opt/venv/bin/python /opt/crib_detector/detector.py --check`
fetches the model and one frame per camera and prints numbers only (frame
width x height and bytes, crop box in pixels, probability, threshold, time
taken).

Offline tests (no camera, broker or real model; onnxruntime is faked):
`python -m pytest crib_detector/tests` with numpy, pillow, aiohttp and pytest
installed.
