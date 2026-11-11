# Home Assistant Add-on: Crib Occupancy Detector

Tells Home Assistant whether a child is in the crib. Every few seconds it
takes one snapshot per camera through Home Assistant's own camera proxy, runs
a small image classifier on it, and publishes the result over MQTT discovery.

**Privacy:** snapshots exist only in memory for one inference and are then
dropped. Nothing writes, logs or publishes image data; only the probability
and the on/off state leave the add-on. The model is trained on another
machine and pulled from a URL on your LAN, so it never has to be published.

Entities per camera (one MQTT device each, `<id>` from the options):

| Entity | What it is |
|---|---|
| `binary_sensor.<id>_occupied` | On once the crib has looked occupied for `on_minutes` (default 5); off once it has looked empty for `off_minutes` (default 2). |
| `sensor.<id>_probability` | The latest raw P(occupied), % (diagnostic). |
| `sensor.<id>_model` | The model version in use (diagnostic). |

See [DOCS.md](./DOCS.md) for setup and the model format.
