# Roborock Local Companion — Documentation

## What it does, and does not do

- **Does:** control the robot over the LAN: start / pause / stop / dock /
  locate, room cleaning, the robot's per-room settings, suction, switching
  the active saved map, deleting the unnamed maps the robot auto-builds when
  it gets lost, and robot state, consumables, clean history and dock settings.
  Exposed via MQTT discovery.
- **Never contacts the Roborock cloud.** The only credential it needs is the
  robot's `local_key`, which is static for the life of the pairing.

## Requirements

- A Roborock robot that speaks the V1/L01 local protocol (tested: Saros 20,
  firmware 02.55.44), reachable from the add-on network on TCP port 58867.
- The **Mosquitto broker** add-on, and the **MQTT integration** set up in Home
  Assistant (Settings → Devices & services; Mosquitto is auto-discovered — if
  you previously ignored it, un-ignore it). Discovery messages are retained,
  so the entities appear as soon as the integration connects.

## Obtaining `device_id` and `local_key` (one time)

The local key is only handed out by Roborock's cloud, so this step needs the
robot registered to a Roborock account and a machine with internet. It is a
one-off; the robot itself does not need internet, then or later.

Using the [`python-roborock`](https://github.com/python-roborock/python-roborock)
CLI (`pip install "python-roborock[cli]"`):

```
roborock login --email <account email>          # prompts for the emailed code
roborock list-devices                           # prints name -> device_id (duid)
python3 -c 'import json;[print(d["name"],d["duid"],d["localKey"]) for d in json.load(open(".roborock"))["cacheData"]["homeData"]["devices"]]'
```

Treat the output, and the CLI's `~/.roborock` file, as secrets. Once the key
is in the add-on options you may delete that file.

The robot's IP is in your router's DHCP table, or in the Matter integration's
device diagnostics (`0/51/0` network interfaces). Give the robot a DHCP
reservation.

## Configuration

```yaml
robot_ip: 192.168.1.50
device_id: "<duid from list-devices>"
local_key: "<16 chars>"
device_name: Roborock Saros 20      # device name in HA; entity IDs derive from it
suggested_area: Living room         # area HA proposes when the device is first discovered
poll_interval: 30                   # seconds between status reads
maps_refresh_interval: 600          # seconds between saved-map list reads
room_settings_map: Main floor       # map whose rooms get per-room setting controls
discovery_prefix: homeassistant
mqtt_host: ""                       # leave empty to use the Supervisor's MQTT service
mqtt_port: 1883
mqtt_username: ""
mqtt_password: ""
log_level: info
```

| Option | Type | Description |
|---|---|---|
| `robot_ip` | string | Robot's LAN IP. |
| `device_id` | string | Roborock `duid`. |
| `local_key` | password | Robot's local encryption key. |
| `device_name` | string | Name of the MQTT device in HA. Entity IDs are `<domain>.<slug of this>_<key>`, e.g. `select.roborock_saros_20_active_map`. Default `Roborock vacuum`. |
| `suggested_area` | string | Optional. Area HA assigns on first discovery. |
| `poll_interval` | int 10–600 | Status poll period (s). Default 30. |
| `maps_refresh_interval` | int 60–86400 | Saved-map list refresh (s). Default 600. |
| `room_settings_map` | string | Optional. Saved map whose rooms get per-room setting controls. Default `Main floor`. |
| `discovery_prefix` | string | MQTT discovery prefix. Default `homeassistant`. |
| `mqtt_host` / `mqtt_port` / `mqtt_username` / `mqtt_password` | optional | Explicit broker. Leave `mqtt_host` empty to use Mosquitto via the Supervisor. |
| `log_level` | enum | `debug`, `info`, `warning`, `error`. |

## Room cleaning

The vacuum entity publishes every saved map's rooms as segments with ids
`<map_flag>_<segment>` (e.g. `0_4` = Kitchen on map 0). Segment numbers
restart on each map, so the map flag keeps them unique. Map the segments to
your HA areas once (Settings → the vacuum entity → Areas), then:

- **`vacuum.clean_area`** / the area picker in the vacuum's dialog cleans the
  chosen rooms in the order given, **each with its own per-room settings**
  (the robot's "custom" mode).
- **`vacuum.send_command`** `clean_rooms` cleans rooms with **one** mode for
  the whole job, which suits scripts:

  ```yaml
  action: vacuum.send_command
  target:
    entity_id: vacuum.roborock_saros_20
  data:
    command: clean_rooms
    params:
      rooms: [Entrance, Hallway, Kitchen]   # the robot's room names
      suction: balanced    # off | quiet | balanced | turbo | max | max_plus
      water: "off"         # off | slight | low | gentle | medium | moderate | high | extreme
      route: standard      # standard | deep | deep_plus | fast
      passes: 1            # 1-3
  ```

  Defaults: Balanced, water Off, Standard, 1 pass. Suction and water can't
  both be off.

Both are **refused** (nothing is sent) when the robot is busy, when a room is
not on the active map, or when a name is unknown. `sensor.<name>_last_command`
says which, and why.

## Per-room settings

For every room of `room_settings_map` there are Mode (Vacuum & mop / Vacuum
only / Mop only), Suction, Water flow, Mop route selects and a Passes number.
They are the robot's own per-room ("customize") settings, the same ones the
Roborock app edits, so the robot stays the source of truth: values are read
from it, and re-read after every change.

- Mode is derived: suction Off = Mop only, water Off = Vacuum only. Going back
  to Vacuum & mop restores the last suction / water the room had.
- A room that was never customised shows *unknown* until its first change.
- Changes are refused while the robot is busy, and the controls are
  unavailable while the robot is on another map (the segment numbers there
  belong to other rooms).
- The robot replaces its whole per-room list on every write. The add-on always
  re-reads the list and writes every room back, and refuses to write if the
  robot returns an empty list.

## Behaviour

- Switching the map is **refused while the robot is busy** (cleaning, docking,
  washing the mop, …). The select snaps back to the real value and the add-on
  log says why. Dock the robot first.
- The robot's `map_status` is the source of truth; the select is re-read after
  every switch. Matter's `SupportedAreas` attribute does **not** tell you the
  active map (it can list every map's rooms at once), so don't infer from it.
- The device is marked unavailable when the robot cannot be reached; the
  bridge reconnects with backoff.
- Maps with an empty name (freshly auto-built ones) show as `Map <n>`.
- **Delete unnamed maps** (`button`): a confused robot builds a new, unnamed
  map instead of relocalising. Pressing the button deletes every saved map
  whose name is empty, except the active one (switch away first). Named maps
  are never touched. Refused while the robot is busy. The map list and the
  select's options refresh right after. This is a tidy-up, not a cure: keep the
  robot on the right map and start it from the dock to avoid new ones.
- **Consumables, history, dock** are read every 5 minutes, and immediately
  after a job ends. "Hours left" goes negative when a consumable is overdue.
  The reset buttons send the robot's own `reset_consumable`, the same thing the
  app does after you clean or replace a part. Dock settings are read-only for
  now; mop wash modes the library does not name show as `code <n>`.

## Example: refuse the nightly clean on the wrong map

```yaml
condition:
  - condition: state
    entity_id: select.roborock_saros_20_active_map
    state: Main floor
```

## Troubleshooting

- **`no reply within 10s`** right after install: wrong `local_key` or
  `device_id`. The TCP handshake succeeds without the key; the first encrypted
  command does not.
- **`Failed to connect to <ip>:58867`**: the robot is off, asleep off-dock, or
  on a VLAN the add-on cannot reach. Only the robot's *internet* should be
  blocked, not LAN traffic from the HA host.
- **Entities never appear**: the MQTT integration is not set up in HA, or a
  different `discovery_prefix` is configured there.
- A factory reset or re-pairing the robot to the account generates a new
  `local_key`; repeat the one-time step.
