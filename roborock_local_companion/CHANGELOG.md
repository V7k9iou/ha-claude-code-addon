# Changelog

## 0.2.2 — map switching after a paused return trip

- Pressing the robot's home button again while it drives back pauses the
  return trip, and the robot then rejects every map switch with
  `-10007 invalid status`. The add-on now cancels such a paused return
  (Stop) and switches. A robot that is still moving or mid-job is refused,
  never stopped.
- Map switch refusals and results are reported in the Last command sensor,
  with a plain hint instead of the raw `-10007` error.

## 0.2.1 — no more "unavailable" blips

- The robot ends each local session about 270 s after it connects, and
  sometimes stops answering instead of closing. A command sent then failed
  after 10 s and greyed out every entity. The add-on now opens a fresh
  session every 200 s, retries a timed-out request once on a fresh session,
  and no longer marks the device offline because one command failed.

## 0.2.0 — local control: vacuum entity, room cleaning, per-room settings

- New `vacuum.<name>` entity, fully local: start / pause / stop / dock /
  locate / suction, and room cleaning through HA's area picker
  (`vacuum.clean_area`). Each room is cleaned with its own per-room settings.
  Segment ids are `<map_flag>_<segment>`; map them to your areas once.
- `vacuum.send_command` `clean_rooms`: clean named rooms with one explicit
  suction / water / route / passes (for scripts).
- Per-room Mode / Suction / Water flow / Mop route / Passes controls for the
  rooms of one map (new option `room_settings_map`, default `Main floor`),
  stored on the robot itself and re-read after every change.
- `Battery` and `Last command` sensors; commands that can't be carried out
  (robot busy, room not on the active map, unknown room) are refused and
  reported instead of being sent.
- Status is polled every 5 s for 3 minutes after a command and every 10 s
  while a job runs.

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
