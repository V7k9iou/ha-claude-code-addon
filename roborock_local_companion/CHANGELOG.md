# Changelog

## 0.1.0 — initial release

- Local-only bridge for a Matter-paired Roborock: active-map `select`, robot
  state / error / last-clean sensors via MQTT device discovery.
- Talks to the robot over its LAN protocol only (TCP 58867); no Roborock cloud,
  no account token, works with the robot's internet blocked.
- Map switches are refused while the robot is mid-job.
- `Delete unnamed maps` button: removes the auto-built, unnamed maps a lost
  robot creates; never the active map, never a named one; refused mid-job.
- Pinned `python-roborock 7.15.2`, `aiomqtt 2.5.1`.
