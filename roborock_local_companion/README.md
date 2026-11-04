# Home Assistant Add-on: Roborock Local Companion

Local-only companion for a Roborock vacuum that you control through the
**Matter** integration. Matter covers starting, stopping, docking, room
(segment) cleaning and clean modes, but it has no command to switch between
the robot's saved maps. When the robot ends up on the wrong map, every
`vacuum.clean_area` call silently matches nothing.

This add-on fills that gap over the robot's own LAN protocol (TCP port 58867)
and exposes the result to Home Assistant via MQTT discovery. It never talks to
the Roborock cloud and does not need your Roborock account — only the robot's
static `local_key`, obtained once (see [DOCS.md](./DOCS.md)). It keeps working
with the robot's internet access blocked.

Entities (one MQTT device):

| Entity | What it is |
|---|---|
| `select.<name>_active_map` | The robot's active saved map. Pick another to switch. Refused while the robot is mid-job. |
| `sensor.<name>_robot_state` | `charging`, `segment_cleaning`, `washing_the_mop`, … with every raw status field as attributes. |
| `sensor.<name>_error` | Robot error name (diagnostic). |
| `sensor.<name>_last_clean_duration` | Minutes (diagnostic). |
| `sensor.<name>_last_clean_area` | m² (diagnostic). |
| `button.<name>_delete_unnamed_maps` | Deletes the unnamed maps the robot auto-builds when it loses its position. Never touches a named map or the active map. |

Everything Matter already does stays on Matter, by design: one control path per
function.
