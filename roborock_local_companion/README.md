# Home Assistant Add-on: Roborock Local Companion

Local-only control for a Roborock vacuum over the robot's own LAN protocol
(TCP port 58867), exposed to Home Assistant via MQTT discovery: a full vacuum
entity with room cleaning through HA's area picker, the robot's per-room
cleaning settings, map switching (which Matter cannot do), and status,
consumables and history. It never talks to
the Roborock cloud and does not need your Roborock account — only the robot's
static `local_key`, obtained once (see [DOCS.md](./DOCS.md)). It keeps working
with the robot's internet access blocked.

Entities (one MQTT device):

| Entity | What it is |
|---|---|
| `vacuum.<name>` | Start / pause / stop / dock / locate / suction. Room cleaning through HA's area picker (`vacuum.clean_area`): each room is cleaned with its own per-room settings. `vacuum.send_command` `clean_rooms` cleans named rooms with one explicit mode. |
| `select.<name>_<room>_{mode,suction,water,route}`, `number.<name>_<room>_passes` | The robot's own per-room settings for one map (default "Main floor"). Unavailable while the robot is on another map. |
| `sensor.<name>_battery`, `sensor.<name>_last_command` | Battery; the result of the last command, including why one was refused. |
| `select.<name>_active_map` | The robot's active saved map. Pick another to switch. Refused while the robot is mid-job. |
| `sensor.<name>_robot_state` | `charging`, `segment_cleaning`, `washing_the_mop`, … with every raw status field as attributes. |
| `sensor.<name>_error` | Robot error name (diagnostic). |
| `button.<name>_delete_unnamed_maps` | Deletes the unnamed maps the robot auto-builds when it loses its position. Never touches a named map or the active map. |
| `sensor.<name>_{main_brush,side_brush,filter,sensor}_left` | Hours left on each consumable, with a `button.<name>_reset_*` for each (diagnostic/config). |
| `sensor.<name>_last_clean_{start,duration,area,type,finish_reason}`, `binary_sensor.<name>_last_clean_complete` | The robot's own record of the last job. |
| `sensor.<name>_total_clean_{count,time,area}`, `sensor.<name>_dust_collection_count` | Lifetime totals. |
| `sensor.<name>_{dust_collection_mode,mop_wash_mode,mop_wash_interval,smart_mop_wash}` | Dock settings, read-only (diagnostic). |

A Matter pairing of the same robot can stay in place as a fallback; this add-on
does not need it.
