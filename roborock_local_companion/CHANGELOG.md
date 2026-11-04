# Changelog

## 0.1.1 — fix image build

- The image build failed on HA OS with `musl-1.2.5-r23 breaks musl-dev-1.2.5-r24`:
  Alpine's `musl-dev` had moved ahead of the musl pinned in the base image.
  The compiler was only a safety net; every dependency ships a prebuilt Alpine
  wheel, so the build deps are gone and pip now runs with `--only-binary=:all:`.

## 0.1.0 — initial release

- Local-only bridge for a Matter-paired Roborock: active-map `select`, robot
  state / error / last-clean sensors via MQTT device discovery.
- Talks to the robot over its LAN protocol only (TCP 58867); no Roborock cloud,
  no account token, works with the robot's internet blocked.
- Map switches are refused while the robot is mid-job.
- `Delete unnamed maps` button: removes the auto-built, unnamed maps a lost
  robot creates; never the active map, never a named one; refused mid-job.
- Consumables (hours left + reset buttons), clean history (last record and
  lifetime totals) and read-only dock settings, refreshed every 5 min and right
  after a job ends.
- Pinned `python-roborock 7.15.2`, `aiomqtt 2.5.1`.
