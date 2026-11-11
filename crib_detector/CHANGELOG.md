# Changelog

## 0.1.0 — first version

- Per-camera crib occupancy from Home Assistant camera snapshots and an ONNX
  classifier pulled from a LAN URL (JSON sidecar + model file, cached in
  `/data`).
- `binary_sensor.<id>_occupied` with on/off hysteresis, plus raw probability
  and model version sensors, over MQTT discovery.
- Images are only held in memory for one inference.
