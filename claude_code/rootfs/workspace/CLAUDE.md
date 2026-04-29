# Home Assistant operator briefing

You are running inside a Home Assistant add-on container on the user's Home
Assistant OS host (typically a Raspberry Pi). The user has SSH'd in and is
using you as an interactive operator for Home Assistant — creating
automations, scenes, scripts, dashboards, querying device state and history,
and generally doing what a code-savvy admin would do.

This file is your standing context. Keep it in mind across the session.

## Filesystem

The host's Home Assistant directories are mounted into this container. Work
through `/workspace/` (which has symlinks to all of these) or hit the
canonical paths directly:

| Path                    | Source                  | Mode | Notes |
|-------------------------|-------------------------|------|-------|
| `/homeassistant/`       | HA's `/config`          | rw   | configuration.yaml, automations, scenes, etc. |
| `/share/`               | shared folder           | rw   | accessible from HA + other add-ons |
| `/media/`               | media folder            | rw   | media library |
| `/addon_configs/`       | all add-on configs      | rw   | per-add-on persistent config |
| `/addons/`              | local add-on dir        | ro   | source of local add-ons (don't edit here) |
| `/ssl/`                 | SSL certs               | ro   | Let's Encrypt etc. |
| `/backup/`              | HA backups              | ro   | inspect, don't write |
| `/data/`                | this add-on's storage   | rw   | YOUR Claude config, SSH keys, persistent state |

Key files inside `/homeassistant/`:

- `configuration.yaml` — main config. Edits require `ha-restart` to apply.
- `automations.yaml`, `scripts.yaml`, `scenes.yaml` — declarative configs.
  Edits can be applied without restart via `automation.reload`,
  `script.reload`, `scene.reload` services.
- `secrets.yaml` — values referenced as `!secret name` in YAML. Treat as
  sensitive; never echo to logs.
- `home-assistant_v2.db` — SQLite recorder DB. **Read-only access only.** HA
  is writing to this constantly; opening it read-write will conflict.
- `.storage/` — JSON files HA's runtime owns (registry, dashboards, etc.).
  **Do not hand-edit these** unless HA is stopped — bad edits corrupt state.
  Prefer the WebSocket / REST API for changes that touch the registries.
- `custom_components/` — third-party integrations.
- `themes/`, `www/` — UI customization.

## Home Assistant API

The container has `$SUPERVISOR_TOKEN` set in the environment. Three endpoints
are reachable on the `supervisor` hostname:

- **HA Core REST**: `http://supervisor/core/api/...`
- **HA Core WebSocket**: `ws://supervisor/core/websocket`
- **Supervisor**: `http://supervisor/...` (manage add-ons, backups, etc.)

The role granted is `manager`, so you can install/configure/start/stop other
add-ons and trigger backups, but not arbitrary host commands.

### Helpers (already on `$PATH`)

```
ha-state <entity_id>                 # GET /api/states/<entity_id>
ha-call-service <domain> <service> [json]
ha-list-entities [regex]             # filter by pattern
ha-check-config                      # validate configuration.yaml
ha-restart                           # validates then restarts HA Core
ha-token                             # prints SUPERVISOR_TOKEN
```

The `ha` shell alias also exists: `ha GET http://supervisor/core/api/states`.

### Common direct calls

```bash
# Reload automations after editing automations.yaml
ha-call-service automation reload

# Reload scripts/scenes
ha-call-service script reload
ha-call-service scene reload

# Trigger a full backup (Supervisor API, not Core)
curl -sSL -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    http://supervisor/backups/new/full \
    -H "Content-Type: application/json" \
    -d '{"name":"pre-claude-edit"}'

# List installed add-ons
curl -sSL -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    http://supervisor/addons | jq .
```

## Workflow rules

These are non-negotiable safety habits in this environment.

1. **Always validate before restarting HA Core.** Use `ha-check-config` (or
   `ha-restart`, which runs it for you). A broken `configuration.yaml`
   leaves the user with a non-booting HA.

2. **Take a backup before risky changes.** Hit
   `POST http://supervisor/backups/new/full` (Supervisor API) before mass
   edits to `configuration.yaml`, before deleting integrations, before
   restoring from a previous backup, etc.

3. **Prefer reload services over restart.** `automation.reload`,
   `script.reload`, `scene.reload`, `template.reload`, and the
   `homeassistant.reload_config_entry` service can apply most changes
   without bouncing HA Core.

4. **Don't hand-edit `.storage/`.** That directory is HA's runtime state —
   the area registry, device registry, entity registry, lovelace
   dashboards, integration config entries. Use the WebSocket API
   (`config/area_registry/*`, `config/entity_registry/*`, etc.) instead. If
   you must edit a file there, stop HA Core first.

5. **Open the recorder DB read-only.** `sqlite3 -readonly
   /homeassistant/home-assistant_v2.db ...`. The schema:
   `states`, `state_attributes`, `events`, `event_data`, `statistics`,
   `statistics_short_term`. Timestamps are unix epoch float in
   `last_updated_ts` / `time_fired_ts`. If the user has switched the
   recorder to MariaDB/Postgres, this file may be stale or empty — detect
   that and tell them.

6. **Don't echo `$SUPERVISOR_TOKEN` or `secrets.yaml` contents into chat
   logs or files outside `/data` or `/homeassistant`.** Treat them as you
   would API keys.

7. **Keep your own state in `/data`.** `/data/claude/` has your settings
   and login. Anything you stash elsewhere on the container's filesystem
   is wiped on every add-on update.

## Common tasks — quick recipes

### Create a new automation

1. Read `/homeassistant/automations.yaml`.
2. Append the new entry.
3. `ha-call-service automation reload`
4. Verify: `ha-list-entities '^automation\.'`

### Create a new scene

1. Edit `/homeassistant/scenes.yaml` (or whichever file is referenced by
   `scene: !include` in `configuration.yaml`).
2. `ha-call-service scene reload`

### Generate a report on devices in a given area

1. Get area + device + entity registries via WebSocket
   (`config/area_registry/list`, `config/device_registry/list`,
   `config/entity_registry/list`) — these are richer than `/api/states`.
2. Cross-reference: device.area_id → entities → current states.
3. Output as a markdown table.

### Find why an automation didn't fire

1. Get its last triggered: `ha-state automation.<slug>` →
   `attributes.last_triggered`.
2. Query the logbook: `GET /api/logbook?entity=automation.<slug>`.
3. Check trace data:
   `GET /api/config/automation/config/<id>/trace` (newer HA versions).

## Architecture invariants

- This container is rebuilt from scratch on every add-on update. Only
  `/data` survives. `~/.bash_history` and any files outside `/data` /
  `/homeassistant` / `/share` / `/media` / `/addon_configs` / `/backup` /
  `/ssl` are gone after each update.
- The user's Claude Code login (after `claude /login`) is stored under
  `$CLAUDE_CONFIG_DIR=/data/claude` so it survives rebuilds.
- The SSH host keys are in `/data/ssh/` so the operator's known_hosts
  fingerprint stays stable across updates.
- The Supervisor token rotates per container start, so don't cache it in a
  file — always read `$SUPERVISOR_TOKEN`.
