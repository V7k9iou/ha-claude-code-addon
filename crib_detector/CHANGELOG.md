# Changelog

## 0.1.0 — first version

- Per-camera crib occupancy from Home Assistant camera snapshots and an ONNX
  classifier pulled from the trainer on the LAN (JSON sidecar +
  `<version>.onnx`, bearer token, sha256-checked, cached in `/data`).
- Per-camera crop box and threshold from the sidecar, looked up by each
  camera's `model_key`. A missing key rejects the model.
- Optional telemetry: P(occupied) per camera POSTed back to the trainer, numbers
  only.
- `binary_sensor.<id>_occupied` with on/off hysteresis, plus raw probability
  and model version sensors, over MQTT discovery. Unknown after a start until
  a full window has been seen, so a restart never reports "empty".
- Entities go unavailable if the add-on stops or crashes (MQTT last will).
- Model downloads and loads run beside detection, never pausing it.
- Images are only held in memory for one inference.
