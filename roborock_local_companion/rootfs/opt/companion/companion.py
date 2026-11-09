#!/opt/venv/bin/python
"""Roborock Local Companion.

Controls a Roborock robot from Home Assistant using ONLY the robot's LAN
protocol (TCP 58867) on the robot side and MQTT discovery on the Home
Assistant side. No Roborock cloud, no account token: the robot's static
``local_key`` is all it needs.

Entities created (all under one MQTT device):
  vacuum  Vacuum               -- start / pause / stop / dock / locate / suction,
                                  plus room cleaning: HA's area picker sends
                                  segments, each cleaned with its own per-room
                                  settings; ``send_command clean_rooms`` cleans
                                  named rooms with one explicit mode (scripts)
  sensor  Battery, Last command
  per-room settings (one map, default "Main floor")
          Mode / Suction / Water flow / Mop route selects and a Passes number
          for every room. Stored ON THE ROBOT (its per-room "customize"
          settings, the ones the Roborock app edits); the robot is the source
          of truth and is re-read after every write.
  select  Active map           -- pick a saved map; refused while the robot is busy,
                                  a return trip left paused is cancelled first
  sensor  Robot state          -- charging / segment_cleaning / ... with all raw
                                  status fields as attributes
  sensor  Error                -- robot error name (diagnostic)
  button  Delete unnamed maps  -- removes auto-built maps that have no name;
                                  never the active map, never a named one
  sensors Consumables          -- hours left on main brush, side brush, filter,
                                  sensors, plus a reset button for each
  sensors History              -- last clean start / duration / area / outcome,
                                  lifetime count / time / area / bin empties
  sensors Dock settings        -- dust-collection mode, mop wash mode and
                                  interval (read-only)

Segment ids are published as ``<map_flag>_<segment>``: segment numbers restart
on every map, so the map flag keeps them unique, and the list stays stable
when the active map changes. Segments that are not on the active map are
refused rather than sent (the robot would match nothing and wander).

Configuration is by environment variables (set by the s6 ``run`` script from
the add-on options). ``--dry-run`` connects to the robot, prints the discovery
and state payloads, and exits without touching MQTT.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from roborock.data.v1.v1_code_mappings import RoborockDockDustCollectionModeCode
from roborock.data.v1.v1_containers import CleanRecord, CleanSummary, Consumable, MultiMapsList, StatusV2
from roborock.devices.transport.local_channel import LocalChannel
from roborock.exceptions import RoborockException
from roborock.protocols.v1_protocol import RequestMessage, decode_rpc_response
from roborock.roborock_message import RoborockMessageProtocol

_LOGGER = logging.getLogger("companion")

RPC_TIMEOUT = 6.0  # per attempt; the robot normally answers in well under a second
# The robot ends each local session ~270 s after it connects (measured on a
# Saros 20, keep-alive pings or not). Sometimes it closes cleanly; sometimes it
# just stops answering, and a command sent then hangs until RPC_TIMEOUT. So the
# bridge opens a fresh session before the robot drops the old one, and retries
# a timed-out request once on a fresh session.
SESSION_MAX_AGE = 200.0  # < 270 s minus one 30 s poll interval, with margin
ROBOT_RECONNECT_MIN = 5.0
ROBOT_RECONNECT_MAX = 300.0
MQTT_RECONNECT = 10.0
SWITCH_SETTLE = 3.0
STOP_SETTLE = 6.0  # max wait for app_stop to clear in_returning (took ~5 s on a Saros 20)
# V1 error reply "invalid status": the robot has locked the action. Seen on
# load_multi_map while a return trip was left paused (in_returning=1).
INVALID_STATUS = -10007
INFO_INTERVAL = 300.0  # consumables / history / dock settings refresh (also right after a job ends)
FAST_POLL = 5.0  # status poll period right after a command, so HA sees the job start promptly
FAST_POLL_FOR = 180.0
BUSY_POLL = 10.0  # status poll period while a job runs

CONSUMABLE_RESET_KEYS = {
    "main_brush": "main_brush_work_time",
    "side_brush": "side_brush_work_time",
    "filter": "filter_work_time",
    "sensor": "sensor_dirty_time",
}
WASH_TOWEL_MODES = {0: "light", 1: "balanced", 2: "deep"}

# Per-room / per-job cleaning settings. Labels are what HA shows; scripts may
# also pass the normalised form ("max_plus", "deep_plus", "high").
SUCTION = {"Off": 105, "Quiet": 101, "Balanced": 102, "Turbo": 103, "Max": 104, "Max+": 108}
WATER = {"Off": 200, "Slight": 221, "Low": 225, "Gentle": 230, "Medium": 235,
         "Moderate": 240, "High": 245, "Extreme": 250}
ROUTE = {"Standard": 300, "Deep": 301, "Deep+": 303, "Fast": 304}
MODES = ("Vacuum & mop", "Vacuum only", "Mop only")
# Water-slider firmware stores the slider position (1-30) as 220 + position;
# the app groups positions into named ranges, keyed by their last position.
WATER_SLIDE_RANGES = ((3, "Slight"), (7, "Low"), (12, "Gentle"), (17, "Medium"),
                      (22, "Moderate"), (27, "High"), (30, "Extreme"))
WATER_LEGACY = {201: "Low", 202: "Medium", 203: "High"}
# "Custom" = use each room's own settings (fan / water / route codes).
CUSTOM_FAN, CUSTOM_WATER, CUSTOM_ROUTE = 106, 204, 302
DEFAULT_SUCTION, DEFAULT_WATER, DEFAULT_ROUTE = 102, 235, 300
FAN_SPEEDS = {"off": 105, "quiet": 101, "balanced": 102, "turbo": 103, "max": 104,
              "max_plus": 108, "custom": CUSTOM_FAN}

# HA vacuum states for each RoborockStateCode. Dock-side states (charging,
# emptying, washing the mop) read "cleaning" while a job is still in progress.
VACUUM_DOCK_STATES = {8, 14, 22, 23, 25, 100, 202, 6310}
VACUUM_STATES = {
    1: "cleaning", 4: "cleaning", 5: "cleaning", 7: "cleaning", 11: "cleaning",
    16: "cleaning", 17: "cleaning", 18: "cleaning", 26: "cleaning", 29: "cleaning",
    6: "returning", 15: "returning",
    10: "paused",
    9: "error", 12: "error",
    2: "idle", 3: "idle", 13: "idle",
}

# RoborockStateCode values during which a map switch would interrupt a job.
BUSY_STATES = {
    1,  # starting
    4,  # remote_control_active
    5,  # cleaning
    6,  # returning_home
    7,  # manual_mode
    11,  # spot_cleaning
    13,  # shutting_down
    14,  # updating
    15,  # docking
    16,  # going_to_target
    17,  # zoned_cleaning
    18,  # segment_cleaning
    22,  # emptying_the_bin
    23,  # washing_the_mop
    26,  # going_to_wash_the_mop
    29,  # mapping
}


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    robot_ip: str
    duid: str
    local_key: str
    device_name: str
    suggested_area: str
    poll_interval: float
    maps_refresh_interval: float
    discovery_prefix: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    addon_version: str
    room_settings_map: str

    @classmethod
    def from_env(cls) -> Config:
        def req(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                sys.exit(f"{name} is required")
            return value

        return cls(
            robot_ip=req("ROBOT_IP"),
            duid=req("ROBOT_DUID"),
            local_key=req("ROBOT_LOCAL_KEY"),
            device_name=os.environ.get("DEVICE_NAME", "Roborock vacuum").strip() or "Roborock vacuum",
            suggested_area=os.environ.get("SUGGESTED_AREA", "").strip(),
            poll_interval=float(os.environ.get("POLL_INTERVAL", "30")),
            maps_refresh_interval=float(os.environ.get("MAPS_REFRESH_INTERVAL", "600")),
            discovery_prefix=os.environ.get("DISCOVERY_PREFIX", "homeassistant").strip() or "homeassistant",
            mqtt_host=os.environ.get("MQTT_HOST", "").strip(),
            mqtt_port=int(os.environ.get("MQTT_PORT", "1883") or 1883),
            mqtt_username=os.environ.get("MQTT_USERNAME", ""),
            mqtt_password=os.environ.get("MQTT_PASSWORD", ""),
            addon_version=os.environ.get("ADDON_VERSION", "dev"),
            room_settings_map=os.environ.get("ROOM_SETTINGS_MAP", "Main floor").strip() or "Main floor",
        )


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "roborock"


# ----------------------------------------------------------------------------
# Robot: one local TCP channel, plain V1 RPC on top of it
# ----------------------------------------------------------------------------


class RobotError(RoborockException):
    """The robot answered with an error: a real refusal, not a lost message."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class Robot:
    """Owns the single local connection to the robot and serialises RPCs."""

    def __init__(self, ip: str, duid: str, local_key: str) -> None:
        self._ip = ip
        self._duid = duid
        self._local_key = local_key
        self._channel: LocalChannel | None = None
        self._connected_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._channel is not None and self._channel.is_connected

    async def connect(self) -> None:
        recycle = self.connected
        if recycle:
            if time.monotonic() - self._connected_at < SESSION_MAX_AGE:
                return
            _LOGGER.debug("Recycling the robot session before the robot drops it")
        self.close()
        channel = LocalChannel(self._ip, self._local_key, self._duid)
        await channel.connect()
        self._channel = channel
        self._connected_at = time.monotonic()
        (_LOGGER.debug if recycle else _LOGGER.info)(
            "Connected to %s:58867 (local protocol %s)", self._ip, channel.protocol_version.value
        )

    def close(self) -> None:
        if self._channel is not None:
            try:
                # The library warns when closing a session the robot already
                # ended; there is nothing to close then.
                if self._channel.is_connected:
                    self._channel.close()
            except Exception:  # noqa: BLE001 - closing a dead transport must never raise
                pass
            self._channel = None

    async def rpc(self, method: str, params=None, retry: bool = True):
        """One request/response. A timeout or a dead session gets one retry on a
        fresh session (``retry=False`` for requests that must not run twice).
        An error reply from the robot is never retried."""
        async with self._lock:
            try:
                return await self._rpc_once(method, params)
            except RoborockException as err:
                if not retry or isinstance(err, RobotError):
                    raise
                _LOGGER.info("%s: %s; retrying on a fresh session", method, err)
                self.close()
                return await self._rpc_once(method, params)

    async def _rpc_once(self, method: str, params):
        await self.connect()
        channel = self._channel
        assert channel is not None
        request = RequestMessage(method, params=params)
        message = request.encode_message(
            RoborockMessageProtocol.GENERAL_REQUEST, version=channel.protocol_version
        )
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_message(incoming) -> None:
            try:
                decoded = decode_rpc_response(incoming)
            except RoborockException:
                return  # keep-alive / hello traffic
            if decoded.request_id == request.request_id and not future.done():
                if decoded.api_error:
                    error = decoded.api_error
                    code = error.get("code") if isinstance(error, dict) else None
                    future.set_exception(RobotError(f"{method}: {error}", code if isinstance(code, int) else None))
                else:
                    future.set_result(decoded.data)

        unsub = await channel.subscribe(on_message)
        try:
            await channel.publish(message)
            return await asyncio.wait_for(future, RPC_TIMEOUT)
        except TimeoutError as err:
            # A silent robot means the session is stale; never reuse it.
            self.close()
            raise RoborockException(f"{method}: no reply within {RPC_TIMEOUT:.0f}s") from err
        finally:
            unsub()

    @staticmethod
    def _first(data):
        return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data

    async def status(self) -> StatusV2:
        return StatusV2.from_dict(self._first(await self.rpc("get_status")))

    async def maps(self) -> MultiMapsList:
        return MultiMapsList.from_dict(self._first(await self.rpc("get_multi_maps_list")))

    async def load_map(self, flag: int):
        return await self.rpc("load_multi_map", [flag])

    async def delete_map(self, flag: int):
        return await self.rpc("del_map", [flag], retry=False)

    async def consumables(self) -> Consumable:
        return Consumable.from_dict(self._first(await self.rpc("get_consumable")))

    async def clean_summary(self) -> CleanSummary:
        return CleanSummary.from_dict(self._first(await self.rpc("get_clean_summary")))

    async def clean_record(self, begin: int) -> CleanRecord:
        return CleanRecord.from_dict(self._first(await self.rpc("get_clean_record", [begin])))

    async def dock_settings(self) -> dict:
        out: dict = {}
        for key, method in (
            ("dust_collection", "get_dust_collection_mode"),
            ("wash_towel", "get_wash_towel_mode"),
            ("smart_wash", "get_smart_wash_params"),
        ):
            try:
                out[key] = self._first(await self.rpc(method))
            except RoborockException as err:
                _LOGGER.debug("%s unsupported: %s", method, err)
        return out

    async def reset_consumable(self, field: str):
        return await self.rpc("reset_consumable", [field])

    async def serial_number(self) -> str | None:
        try:
            data = self._first(await self.rpc("get_serial_number"))
        except RoborockException as err:
            _LOGGER.debug("get_serial_number unsupported: %s", err)
            return None
        return data.get("serial_number") if isinstance(data, dict) else None

    async def customize(self) -> list[dict]:
        """The robot's per-room settings for the active map (rooms never edited are absent)."""
        data = await self.rpc("get_customize_clean_mode")
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    async def set_customize(self, entries: list[dict]):
        # Replaces the WHOLE list: any room left out loses its saved settings.
        return await self.rpc("set_customize_clean_mode", entries)

    async def set_job_mode(self, fan: int, water: int, route: int) -> None:
        await self.rpc("set_custom_mode", [fan])
        await self.rpc("set_water_box_custom_mode", [water])
        await self.rpc("set_mop_mode", [route])

    async def segment_clean(self, segments: list[int], repeat: int = 1):
        return await self.rpc("app_segment_clean", [{"segments": segments, "repeat": repeat}])


# ----------------------------------------------------------------------------
# Map bookkeeping
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Room:
    segment: int
    name: str
    slug: str


@dataclass(frozen=True)
class MapEntry:
    flag: int
    label: str
    rooms: tuple[Room, ...]


def map_entries(maps: MultiMapsList) -> list[MapEntry]:
    """Turn the robot's map list into unique, human-readable select options.

    Roborock lets a map have an empty name (a freshly auto-built one) and keeps
    trailing whitespace in names; HA's select needs distinct, exact strings.
    """
    entries: list[MapEntry] = []
    seen: dict[str, int] = {}
    for info in sorted(maps.map_info or [], key=lambda m: m.map_flag):
        label = (info.name or "").strip() or f"Map {info.map_flag}"
        if label in seen:
            label = f"{label} ({info.map_flag})"
        seen[label] = info.map_flag
        entries.append(MapEntry(flag=info.map_flag, label=label, rooms=map_rooms(info.rooms or [])))
    return entries


def map_rooms(rooms) -> tuple[Room, ...]:
    out: list[Room] = []
    slugs: set[str] = set()
    for room in sorted((r for r in rooms if r.id is not None), key=lambda r: r.id):
        name = (room.iot_name or "").strip() or f"Room {room.id}"
        slug = slugify(name)
        if slug in slugs:
            slug = f"{slug}_{room.id}"
        slugs.add(slug)
        out.append(Room(segment=room.id, name=name, slug=slug))
    return tuple(out)


def segment_id(flag: int, segment: int) -> str:
    return f"{flag}_{segment}"


def find_entry(entries: list[MapEntry], label: str) -> MapEntry | None:
    wanted = label.strip().casefold()
    return next((e for e in entries if e.label.casefold() == wanted), None)


# ----------------------------------------------------------------------------
# Cleaning settings
# ----------------------------------------------------------------------------


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower().replace("+", "_plus")).strip("_")


def lookup(table: dict[str, int], value) -> int:
    """Label or normalised name -> robot code, e.g. ``"Max+"`` or ``"max_plus"`` -> 108."""
    for label, code in table.items():
        if _norm(label) == _norm(value):
            return code
    raise ValueError(f"{value!r} is not one of {', '.join(table)}")


def suction_label(code) -> str | None:
    return next((label for label, c in SUCTION.items() if c == code), None)


def route_label(code) -> str | None:
    return next((label for label, c in ROUTE.items() if c == code), None)


def water_label(code) -> str | None:
    if code == WATER["Off"]:
        return "Off"
    if code in WATER_LEGACY:
        return WATER_LEGACY[code]
    if isinstance(code, int) and 221 <= code <= 250:
        position = code - 220
        return next(label for last, label in WATER_SLIDE_RANGES if position <= last)
    return None


def mode_label(fan, water) -> str | None:
    if fan is None or water is None:
        return None
    if fan == SUCTION["Off"]:
        return "Mop only"
    if water == WATER["Off"]:
        return "Vacuum only"
    return "Vacuum & mop"


def room_view(entry: dict | None) -> dict:
    """What HA shows for one room. Rooms never edited have no entry -> all unknown."""
    if entry is None:
        return {"mode": None, "suction": None, "water": None, "route": None, "passes": None}
    fan, water = entry.get("fan_power"), entry.get("water_box_mode")
    return {
        "mode": mode_label(fan, water),
        "suction": suction_label(fan),
        "water": water_label(water),
        "route": route_label(entry.get("mop_mode")),
        "passes": entry.get("repeat"),
    }


def apply_room_setting(entry: dict, field: str, value: str, remembered: dict) -> dict:
    """Return a copy of one room's customize entry with ``field`` changed.

    ``remembered`` holds the last non-off suction/water for the room, so that
    switching "Vacuum only" back to "Vacuum & mop" restores what was there.
    Raises ValueError on a bad value or a combination the robot can't do.
    """
    new = dict(entry)
    fan_off, water_off = SUCTION["Off"], WATER["Off"]
    if field == "suction":
        new["fan_power"] = lookup(SUCTION, value)
    elif field == "water":
        new["water_box_mode"] = lookup(WATER, value)
    elif field == "route":
        new["mop_mode"] = lookup(ROUTE, value)
    elif field == "passes":
        passes = int(float(value))
        if not 1 <= passes <= 3:
            raise ValueError(f"passes must be 1-3, got {value!r}")
        new["repeat"] = passes
    elif field == "mode":
        mode = next((m for m in MODES if _norm(m) == _norm(value)), None)
        if mode is None:
            raise ValueError(f"{value!r} is not one of {', '.join(MODES)}")
        restore_fan = remembered.get("fan_power", DEFAULT_SUCTION)
        restore_water = remembered.get("water_box_mode", DEFAULT_WATER)
        if mode == "Vacuum only":
            new["water_box_mode"] = water_off
            if new["fan_power"] == fan_off:
                new["fan_power"] = restore_fan
        elif mode == "Mop only":
            new["fan_power"] = fan_off
            if new["water_box_mode"] == water_off:
                new["water_box_mode"] = restore_water
        else:
            if new["fan_power"] == fan_off:
                new["fan_power"] = restore_fan
            if new["water_box_mode"] == water_off:
                new["water_box_mode"] = restore_water
    else:
        raise ValueError(f"unknown setting {field!r}")
    if new["fan_power"] == fan_off and new["water_box_mode"] == water_off:
        raise ValueError("suction and water can't both be Off")
    return new


def new_customize_entry(segment: int) -> dict:
    return {"segment": segment, "fan_power": DEFAULT_SUCTION, "water_box_mode": DEFAULT_WATER,
            "mop_mode": DEFAULT_ROUTE, "repeat": 1, "seq_type": 0}


def vacuum_state(status: StatusV2) -> str:
    code = int(status.state) if status.state is not None else None
    if code in VACUUM_DOCK_STATES:
        return "cleaning" if status.in_cleaning else "docked"
    if code in VACUUM_STATES:
        return VACUUM_STATES[code]
    if code is not None and 6301 <= code <= 6309:
        return "cleaning"
    if status.in_cleaning:
        return "cleaning"
    return "docked" if status.charge_status else "idle"


def fan_speed_name(code) -> str | None:
    return next((name for name, c in FAN_SPEEDS.items() if c == code), None)


# ----------------------------------------------------------------------------
# MQTT payloads
# ----------------------------------------------------------------------------


class Topics:
    def __init__(self, cfg: Config) -> None:
        base = f"roborock_local/{cfg.duid}"
        self.base = base
        self.availability = f"{base}/availability"
        self.state = f"{base}/state"
        self.map_set = f"{base}/map/set"
        self.maps_delete_unnamed = f"{base}/maps/delete_unnamed"
        self.info = f"{base}/info"
        self.consumable_reset = f"{base}/consumable/reset"
        self.vacuum_state = f"{base}/vacuum/state"
        self.vacuum_command = f"{base}/vacuum/command"
        self.vacuum_fan_speed = f"{base}/vacuum/fan_speed/set"
        self.vacuum_send_command = f"{base}/vacuum/send_command"
        self.vacuum_clean_segments = f"{base}/vacuum/clean_segments"
        self.rooms = f"{base}/rooms"
        self.room_set = f"{base}/room/+/+/set"
        self.last_command = f"{base}/last_command"
        self.discovery = f"{cfg.discovery_prefix}/device/roborock_local_{cfg.duid}/config"

    def room_setting(self, segment: int, field: str) -> str:
        return f"{self.base}/room/{segment}/{field}/set"


def discovery_payload(cfg: Config, topics: Topics, entries: list[MapEntry], serial: str | None) -> dict:
    uid = f"roborock_local_{cfg.duid}"
    prefix = slugify(cfg.device_name)

    def entity(key: str, name: str | None, object_id: str | None = None, **extra) -> dict:
        if "availability" not in extra:
            extra["availability_topic"] = topics.availability
        suffix = key if object_id is None else object_id  # "" = the device slug alone
        return {
            "name": name,
            "unique_id": f"{uid}_{key}",
            "default_entity_id": f"{extra.pop('domain')}.{prefix}{'_' + suffix if suffix else ''}",
            **extra,
        }

    device = {
        "identifiers": [uid],
        "name": cfg.device_name,
        "manufacturer": "Roborock",
        "sw_version": f"companion {cfg.addon_version}",
    }
    if serial:
        device["serial_number"] = serial
    if cfg.suggested_area:
        device["suggested_area"] = cfg.suggested_area

    return {
        "device": device,
        "origin": {
            "name": "Roborock Local Companion",
            "sw_version": cfg.addon_version,
            "support_url": "https://github.com/talcum/ha-claude-code-addon",
        },
        "state_topic": topics.state,
        "qos": 0,
        "components": {
            "vacuum": entity(
                "vacuum",
                None,
                object_id="",
                domain="vacuum",
                platform="vacuum",
                state_topic=topics.vacuum_state,
                command_topic=topics.vacuum_command,
                set_fan_speed_topic=topics.vacuum_fan_speed,
                fan_speed_list=list(FAN_SPEEDS),
                send_command_topic=topics.vacuum_send_command,
                clean_segments_command_topic=topics.vacuum_clean_segments,
                supported_features=["start", "pause", "stop", "return_home", "status", "locate",
                                    "fan_speed", "send_command"],
            ),
            "battery": entity(
                "battery",
                "Battery",
                domain="sensor",
                platform="sensor",
                value_template="{{ value_json.battery }}",
                unit_of_measurement="%",
                device_class="battery",
                state_class="measurement",
            ),
            "last_command": entity(
                "last_command",
                "Last command",
                domain="sensor",
                platform="sensor",
                state_topic=topics.last_command,
                value_template="{{ value_json.result[:255] }}",
                json_attributes_topic=topics.last_command,
                icon="mdi:console-line",
                entity_category="diagnostic",
            ),
            "active_map": entity(
                "active_map",
                "Active map",
                domain="select",
                platform="select",
                options=[e.label for e in entries],
                command_topic=topics.map_set,
                value_template="{{ value_json.map_name }}",
                icon="mdi:map-search",
            ),
            "robot_state": entity(
                "robot_state",
                "Robot state",
                domain="sensor",
                platform="sensor",
                value_template="{{ value_json.state }}",
                json_attributes_topic=topics.state,
                icon="mdi:robot-vacuum",
            ),
            "error": entity(
                "error",
                "Error",
                domain="sensor",
                platform="sensor",
                value_template="{{ value_json.error }}",
                icon="mdi:alert-circle-outline",
                entity_category="diagnostic",
            ),
            "delete_unnamed_maps": entity(
                "delete_unnamed_maps",
                "Delete unnamed maps",
                domain="button",
                platform="button",
                command_topic=topics.maps_delete_unnamed,
                payload_press="PRESS",
                icon="mdi:map-minus",
                entity_category="config",
            ),
            **info_components(topics, entity),
            **room_components(topics, entity, find_entry(entries, cfg.room_settings_map)),
        },
    }


def room_components(topics: Topics, entity, settings_map: MapEntry | None) -> dict:
    """Mode / suction / water / route / passes for every room of the settings map.

    Unique ids carry the map flag and segment number, entity ids the robot's
    room name. Unavailable while the robot is on another map, because segment
    numbers restart per map and a write would land on the wrong room.
    """
    if settings_map is None:
        return {}
    availability = [
        {"topic": topics.availability},
        {"topic": topics.rooms, "value_template": "{{ 'online' if value_json.available else 'offline' }}"},
    ]
    comps: dict = {}
    for room in settings_map.rooms:
        key = f"room_{settings_map.flag}_{room.segment}"
        value = f"(value_json.rooms['{room.segment}'] or {{}})"
        common = {"availability": availability, "availability_mode": "all", "state_topic": topics.rooms}
        for field, label, options, icon in (
            ("mode", "mode", list(MODES), "mdi:robot-vacuum-variant"),
            ("suction", "suction", list(SUCTION), "mdi:fan"),
            ("water", "water flow", list(WATER), "mdi:water"),
            ("route", "mop route", list(ROUTE), "mdi:map-marker-path"),
        ):
            comps[f"{key}_{field}"] = entity(
                f"{key}_{field}", f"{room.name} {label}", object_id=f"{room.slug}_{field}",
                domain="select", platform="select", **common,
                options=options, command_topic=topics.room_setting(room.segment, field),
                value_template=f"{{{{ {value}.{field} }}}}", icon=icon,
            )
        comps[f"{key}_passes"] = entity(
            f"{key}_passes", f"{room.name} passes", object_id=f"{room.slug}_passes",
            domain="number", platform="number", **common,
            min=1, max=3, step=1, mode="box",
            command_topic=topics.room_setting(room.segment, "passes"),
            value_template=f"{{{{ {value}.passes }}}}", icon="mdi:repeat",
        )
    return comps


def info_components(topics: Topics, entity) -> dict:
    """Components fed from the slow ``info`` topic: consumables, history, dock."""
    info = {"state_topic": topics.info}

    def sensor(key: str, name: str, field: str, **extra) -> dict:
        return entity(key, name, domain="sensor", platform="sensor",
                      value_template=f"{{{{ value_json.{field} }}}}", **info, **extra)

    comps: dict = {}
    # Consumables: hours left + reset buttons
    for key, name, icon in (
        ("main_brush", "Main brush", "mdi:brush"),
        ("side_brush", "Side brush", "mdi:brush-variant"),
        ("filter", "Filter", "mdi:air-filter"),
        ("sensor", "Sensors", "mdi:eye-outline"),
    ):
        comps[f"{key}_left"] = sensor(
            f"{key}_left", f"{name} left", f"{key}_left_h",
            unit_of_measurement="h", device_class="duration", state_class="measurement",
            icon=icon, entity_category="diagnostic",
        )
        comps[f"reset_{key}"] = entity(
            f"reset_{key}", f"Reset {name.lower()}", domain="button", platform="button",
            command_topic=topics.consumable_reset, payload_press=key,
            icon="mdi:restart", entity_category="config",
        )
    # Last clean
    comps["last_clean_start"] = sensor("last_clean_start", "Last clean start", "last_clean_start",
                                       device_class="timestamp", icon="mdi:clock-start")
    comps["last_clean_duration"] = sensor("last_clean_duration", "Last clean duration", "last_clean_duration_min",
                                          unit_of_measurement="min", device_class="duration",
                                          state_class="measurement", icon="mdi:timer-outline")
    comps["last_clean_area"] = sensor("last_clean_area", "Last clean area", "last_clean_area_m2",
                                      unit_of_measurement="m²", state_class="measurement", icon="mdi:texture-box")
    comps["last_clean_complete"] = entity(
        "last_clean_complete", "Last clean completed", domain="binary_sensor", platform="binary_sensor",
        value_template="{{ 'ON' if value_json.last_clean_complete else 'OFF' }}", icon="mdi:check-circle-outline", **info,
    )
    comps["last_clean_finish_reason"] = sensor("last_clean_finish_reason", "Last clean finish reason",
                                               "last_clean_finish_reason", icon="mdi:flag-checkered",
                                               entity_category="diagnostic")
    comps["last_clean_type"] = sensor("last_clean_type", "Last clean type", "last_clean_type",
                                      icon="mdi:broom", entity_category="diagnostic")
    # Lifetime
    comps["total_clean_count"] = sensor("total_clean_count", "Total cleans", "total_clean_count",
                                        state_class="total_increasing", icon="mdi:counter")
    comps["total_clean_time"] = sensor("total_clean_time", "Total clean time", "total_clean_time_h",
                                       unit_of_measurement="h", device_class="duration",
                                       state_class="total_increasing", icon="mdi:timer-sand")
    comps["total_clean_area"] = sensor("total_clean_area", "Total clean area", "total_clean_area_m2",
                                       unit_of_measurement="m²", state_class="total_increasing", icon="mdi:texture-box")
    comps["dust_collection_count"] = sensor("dust_collection_count", "Bin empties", "dust_collection_count",
                                            state_class="total_increasing", icon="mdi:delete-empty")
    # Dock settings (read-only)
    comps["dust_collection_mode"] = sensor("dust_collection_mode", "Dust collection mode", "dust_collection_mode",
                                           icon="mdi:delete-sweep", entity_category="diagnostic")
    comps["mop_wash_mode"] = sensor("mop_wash_mode", "Mop wash mode", "mop_wash_mode",
                                    icon="mdi:water-sync", entity_category="diagnostic")
    comps["mop_wash_interval"] = sensor("mop_wash_interval", "Mop wash interval", "mop_wash_interval_min",
                                        unit_of_measurement="min", device_class="duration",
                                        icon="mdi:timer-sync-outline", entity_category="diagnostic")
    comps["smart_mop_wash"] = sensor("smart_mop_wash", "Smart mop wash", "smart_mop_wash",
                                     icon="mdi:auto-mode", entity_category="diagnostic")
    return comps


def _hours(seconds) -> float | None:
    return round(seconds / 3600, 1) if seconds is not None else None


def info_payload(consumable: Consumable, summary: CleanSummary, record: CleanRecord | None, dock: dict) -> dict:
    dust = dock.get("dust_collection") or {}
    wash = dock.get("wash_towel") or {}
    smart = dock.get("smart_wash") or {}
    dust_code = dust.get("mode")
    try:
        dust_name = RoborockDockDustCollectionModeCode(dust_code).name if dust_code is not None else None
    except ValueError:
        dust_name = f"code {dust_code}"
    wash_code = wash.get("wash_mode")
    return {
        "main_brush_left_h": _hours(consumable.main_brush_time_left),
        "side_brush_left_h": _hours(consumable.side_brush_time_left),
        "filter_left_h": _hours(consumable.filter_time_left),
        "sensor_left_h": _hours(consumable.sensor_time_left),
        "main_brush_used_h": _hours(consumable.main_brush_work_time),
        "side_brush_used_h": _hours(consumable.side_brush_work_time),
        "filter_used_h": _hours(consumable.filter_work_time),
        "sensor_used_h": _hours(consumable.sensor_dirty_time),
        "total_clean_count": summary.clean_count,
        "total_clean_time_h": _hours(summary.clean_time),
        "total_clean_area_m2": round(summary.clean_area / 1_000_000, 1) if summary.clean_area is not None else None,
        "dust_collection_count": summary.dust_collection_count,
        "last_clean_start": record.begin_datetime.isoformat() if record and record.begin_datetime else None,
        "last_clean_end": record.end_datetime.isoformat() if record and record.end_datetime else None,
        "last_clean_duration_min": round(record.duration / 60, 1) if record and record.duration is not None else None,
        "last_clean_area_m2": record.square_meter_area if record else None,
        "last_clean_complete": bool(record.complete) if record else None,
        "last_clean_error": _enum_name(record.error) if record else None,
        "last_clean_finish_reason": _enum_name(record.finish_reason) if record else None,
        "last_clean_type": _enum_name(record.clean_type) if record else None,
        "last_clean_start_type": _enum_name(record.start_type) if record else None,
        "last_clean_map_flag": record.map_flag if record else None,
        "dust_collection_mode": dust_name,
        "mop_wash_mode": WASH_TOWEL_MODES.get(wash_code, f"code {wash_code}") if wash_code is not None else None,
        "mop_wash_interval_min": round(smart["wash_interval"] / 60) if smart.get("wash_interval") is not None else None,
        "smart_mop_wash": smart.get("smart_wash"),
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _enum_name(value) -> str | None:
    """Enum name without the library's alias suffixes (``finished_cleaning_4`` -> ``finished_cleaning``)."""
    if value is None:
        return None
    name = getattr(value, "name", str(value))
    return re.sub(r"_\d+$", "", name)


def state_payload(status: StatusV2, entries: list[MapEntry]) -> dict:
    flag = status.current_map
    label = next((e.label for e in entries if e.flag == flag), None)
    if label is None and flag is not None:
        label = f"Map {flag}"
    return {
        "map_flag": flag,
        "map_name": label,
        "map_status": status.map_status,
        "state": _enum_name(status.state),
        "state_code": int(status.state) if status.state is not None else None,
        "in_cleaning": status.in_cleaning,
        "in_returning": status.in_returning,
        "battery": status.battery,
        "error": _enum_name(status.error_code) or "none",
        "error_code": int(status.error_code) if status.error_code is not None else None,
        "dock_error_status": _enum_name(status.dock_error_status),
        "charge_status": status.charge_status,
        "clean_time_min": round((status.clean_time or 0) / 60, 1),
        "clean_area_m2": round((status.clean_area or 0) / 1_000_000, 2),
        "fan_power": status.fan_power,
        "water_box_mode": status.water_box_mode,
        "mop_mode": status.mop_mode,
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def is_busy(status: StatusV2) -> bool:
    code = int(status.state) if status.state is not None else None
    return bool(status.in_cleaning) or code in BUSY_STATES


def vacuum_payload(status: StatusV2, entries: list[MapEntry]) -> dict:
    return {
        "state": vacuum_state(status),
        "fan_speed": fan_speed_name(status.fan_power),
        "segments": {segment_id(e.flag, r.segment): r.name for e in entries for r in e.rooms},
    }


def rooms_payload(settings_map: MapEntry | None, customize: list[dict] | None, available: bool) -> dict:
    by_segment = {c.get("segment"): c for c in customize or []}
    rooms = settings_map.rooms if settings_map else ()
    return {
        "available": available,
        "map": settings_map.label if settings_map else None,
        "rooms": {str(r.segment): {"name": r.name, **room_view(by_segment.get(r.segment))} for r in rooms},
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
    }


class Refused(Exception):
    """A command the bridge declined to send; the message goes to Last command."""


# ----------------------------------------------------------------------------
# Bridge
# ----------------------------------------------------------------------------


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.topics = Topics(cfg)
        self.robot = Robot(cfg.robot_ip, cfg.duid, cfg.local_key)
        self.entries: list[MapEntry] = []
        self.serial: str | None = None
        self._maps_refreshed = 0.0
        self._info_refreshed = 0.0
        self._was_busy = False
        self._discovery_sent: str | None = None  # JSON of the last payload published
        self._online = False
        self._mqtt = None
        self._wake = asyncio.Event()
        self._fast_until = 0.0
        self.customize: list[dict] | None = None  # robot's per-room settings, last read
        self._rooms_flag: int | None = None  # map the per-room settings were read on
        self._rooms_refreshed = 0.0
        self._remembered: dict[int, dict] = {}  # last non-off suction / water per segment

    @property
    def settings_map(self) -> MapEntry | None:
        return find_entry(self.entries, self.cfg.room_settings_map)

    # -- publishing helpers ---------------------------------------------------

    async def _publish(self, topic: str, payload, retain: bool = False) -> None:
        if self._mqtt is None:
            return
        data = payload if isinstance(payload, str) else json.dumps(payload)
        await self._mqtt.publish(topic, data, qos=0, retain=retain)

    async def _set_online(self, online: bool) -> None:
        if online != self._online:
            await self._publish(self.topics.availability, "online" if online else "offline", retain=True)
            self._online = online
            _LOGGER.info("Robot %s", "online" if online else "offline")

    async def _publish_discovery(self) -> None:
        payload = discovery_payload(self.cfg, self.topics, self.entries, self.serial)
        encoded = json.dumps(payload, sort_keys=True)
        if encoded != self._discovery_sent:
            await self._publish(self.topics.discovery, payload, retain=True)
            self._discovery_sent = encoded
            _LOGGER.info("Published discovery with maps: %s", ", ".join(e.label for e in self.entries))

    # -- robot side -----------------------------------------------------------

    async def refresh_maps(self, force: bool = False) -> None:
        if not force and self.entries and time.monotonic() - self._maps_refreshed < self.cfg.maps_refresh_interval:
            return
        maps = await self.robot.maps()
        self.entries = map_entries(maps)
        self._maps_refreshed = time.monotonic()
        if self.serial is None:
            self.serial = await self.robot.serial_number()
        await self._publish_discovery()

    async def refresh_info(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._info_refreshed < INFO_INTERVAL:
            return
        consumable = await self.robot.consumables()
        summary = await self.robot.clean_summary()
        record = None
        if summary.records:
            try:
                record = await self.robot.clean_record(summary.records[0])
            except RoborockException as err:
                _LOGGER.debug("get_clean_record failed: %s", err)
        dock = await self.robot.dock_settings()
        await self._publish(self.topics.info, info_payload(consumable, summary, record, dock), retain=True)
        self._info_refreshed = time.monotonic()

    async def poll_once(self) -> StatusV2:
        await self.refresh_maps()
        await self._publish_discovery()  # no-op unless changed or after an MQTT reconnect
        status = await self.robot.status()
        await self._publish(self.topics.state, state_payload(status, self.entries), retain=True)
        await self._publish(self.topics.vacuum_state, vacuum_payload(status, self.entries), retain=True)
        busy = is_busy(status)
        # A job just ended: the clean record and consumables changed, read them now.
        await self.refresh_info(force=self._was_busy and not busy)
        await self.refresh_rooms(status)
        self._was_busy = busy
        await self._set_online(True)
        return status

    async def refresh_rooms(self, status: StatusV2, force: bool = False) -> None:
        """Re-read the robot's per-room settings while it is on the settings map.

        Off that map the controls are published unavailable: segment numbers
        restart per map, so the robot's list would belong to other rooms.
        """
        settings_map = self.settings_map
        on_map = settings_map is not None and status.current_map == settings_map.flag
        due = (force or self._rooms_flag != status.current_map
               or time.monotonic() - self._rooms_refreshed >= INFO_INTERVAL)
        if not due:
            return
        if on_map:
            self.customize = await self.robot.customize()
            for entry in self.customize:
                remembered = self._remembered.setdefault(entry.get("segment"), {})
                if entry.get("fan_power") not in (None, SUCTION["Off"]):
                    remembered["fan_power"] = entry["fan_power"]
                if entry.get("water_box_mode") not in (None, WATER["Off"]):
                    remembered["water_box_mode"] = entry["water_box_mode"]
        self._rooms_refreshed = time.monotonic()
        self._rooms_flag = status.current_map
        await self._publish(self.topics.rooms, rooms_payload(settings_map, self.customize, on_map), retain=True)

    async def _report(self, result: str, ok: bool) -> None:
        (_LOGGER.info if ok else _LOGGER.warning)("Command: %s", result)
        await self._publish(
            self.topics.last_command,
            {"result": result, "ok": ok, "at": datetime.now(UTC).isoformat(timespec="seconds")},
            retain=True,
        )

    def _kick(self) -> None:
        """Poll fast for a while so HA sees the robot react to a command."""
        self._fast_until = time.monotonic() + FAST_POLL_FOR
        self._wake.set()

    async def _require_idle(self, what: str) -> StatusV2:
        status = await self.robot.status()
        if is_busy(status):
            raise Refused(
                f"refused {what}: robot is busy ({_enum_name(status.state)}, in_cleaning={status.in_cleaning});"
                " stop it or let it dock first"
            )
        return status

    def _active_map(self, status: StatusV2) -> MapEntry:
        entry = next((e for e in self.entries if e.flag == status.current_map), None)
        if entry is None:
            raise Refused(f"refused: robot is on unknown map {status.current_map}; refresh maps or switch map")
        return entry

    # -- vacuum commands ------------------------------------------------------

    VACUUM_COMMANDS = {
        "start": "app_start",
        "pause": "app_pause",
        "stop": "app_stop",
        "return_to_base": "app_charge",
        "locate": "find_me",
    }

    async def handle_vacuum_command(self, payload: str) -> None:
        method = self.VACUUM_COMMANDS.get(payload.strip())
        if method is None:
            raise Refused(f"refused: unknown vacuum command {payload!r}")
        result = await self.robot.rpc(method)
        await self._report(f"ok: {payload.strip()} ({method} -> {result})", True)
        self._kick()

    async def handle_fan_speed(self, payload: str) -> None:
        name = payload.strip()
        code = FAN_SPEEDS.get(name)
        if code is None:
            raise Refused(f"refused: unknown fan speed {name!r} (known: {', '.join(FAN_SPEEDS)})")
        if code == CUSTOM_FAN:
            await self.robot.set_job_mode(CUSTOM_FAN, CUSTOM_WATER, CUSTOM_ROUTE)
        else:
            await self.robot.rpc("set_custom_mode", [code])
        await self._report(f"ok: fan speed {name}", True)
        self._kick()

    async def handle_clean_segments(self, payload: str) -> None:
        """HA's area picker: clean these segments, each with its own per-room settings."""
        try:
            ids = [str(i) for i in json.loads(payload)]
        except (ValueError, TypeError) as err:
            raise Refused(f"refused: bad segment list {payload!r}") from err
        if not ids:
            raise Refused("refused: no rooms selected")
        status = await self._require_idle("room clean")
        active = self._active_map(status)
        names = {segment_id(active.flag, r.segment): r for r in active.rooms}
        off_map = [i for i in ids if i not in names]
        if off_map:
            raise Refused(
                f"refused: {', '.join(off_map)} not on the active map {active.label!r}; switch map first"
            )
        rooms = [names[i] for i in ids]
        await self.robot.set_job_mode(CUSTOM_FAN, CUSTOM_WATER, CUSTOM_ROUTE)
        result = await self.robot.segment_clean([r.segment for r in rooms])
        await self._report(f"ok: {', '.join(r.name for r in rooms)} (per-room settings) -> {result}", True)
        self._kick()

    async def handle_send_command(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except ValueError:
            message = {"command": payload.strip()}
        if not isinstance(message, dict):
            message = {"command": str(message)}
        command, params = message.get("command"), message.get("params") or {}
        if command != "clean_rooms":
            raise Refused(f"refused: unknown command {command!r} (known: clean_rooms)")
        await self.clean_rooms(params)

    async def clean_rooms(self, params: dict) -> None:
        """Clean named rooms with ONE explicit mode, e.g. a script's vacuum or mop phase.

        params: rooms (robot room names, in order), suction (default Balanced),
        water (default Off), route (default Standard), passes (default 1).
        """
        if not isinstance(params, dict):
            raise Refused(f"refused: clean_rooms params must be a mapping, got {params!r}")
        wanted = params.get("rooms") or []
        if isinstance(wanted, str):
            wanted = [wanted]
        if not wanted:
            raise Refused("refused: clean_rooms needs rooms")
        try:
            fan = lookup(SUCTION, params.get("suction", "Balanced"))
            water = lookup(WATER, params.get("water", "Off"))
            route = lookup(ROUTE, params.get("route", "Standard"))
            passes = int(params.get("passes", 1))
        except (ValueError, TypeError) as err:
            raise Refused(f"refused: {err}") from err
        if not 1 <= passes <= 3:
            raise Refused(f"refused: passes must be 1-3, got {passes}")
        if fan == SUCTION["Off"] and water == WATER["Off"]:
            raise Refused("refused: suction and water can't both be Off")
        status = await self._require_idle("room clean")
        active = self._active_map(status)
        by_name = {_norm(r.name): r for r in active.rooms} | {r.slug: r for r in active.rooms}
        unknown = [w for w in wanted if _norm(w) not in by_name]
        if unknown:
            raise Refused(
                f"refused: {', '.join(map(str, unknown))} not on the active map {active.label!r}"
                f" (rooms: {', '.join(r.name for r in active.rooms)})"
            )
        rooms = [by_name[_norm(w)] for w in wanted]
        await self.robot.set_job_mode(fan, water, route)
        result = await self.robot.segment_clean([r.segment for r in rooms], passes)
        mode = f"{suction_label(fan)} / water {water_label(water)} / {route_label(route)} / x{passes}"
        await self._report(f"ok: {', '.join(r.name for r in rooms)} ({mode}) -> {result}", True)
        self._kick()

    # -- per-room settings ----------------------------------------------------

    async def handle_room_setting(self, topic: str, payload: str) -> None:
        # topic: roborock_local/<duid>/room/<segment>/<field>/set
        *_, segment_text, field, _set = topic.split("/")
        settings_map = self.settings_map
        try:
            segment = int(segment_text)
            room = next(r for r in settings_map.rooms if r.segment == segment) if settings_map else None
        except (ValueError, StopIteration):
            room = None
        if room is None:
            raise Refused(f"refused: unknown room segment {segment_text!r}")
        try:
            status = await self._require_idle(f"{room.name} {field} change")
            if status.current_map != settings_map.flag:
                raise Refused(f"refused: robot is not on {settings_map.label!r}; per-room settings belong to that map")
            # set_customize_clean_mode REPLACES the whole list, so always start
            # from a fresh read and write every room back.
            current = await self.robot.customize()
            if not current and self.customize:
                raise Refused("refused: robot returned no per-room settings; not overwriting the saved ones")
            entries = [dict(e) for e in current]
            index = next((i for i, e in enumerate(entries) if e.get("segment") == segment), None)
            base = entries[index] if index is not None else new_customize_entry(segment)
            try:
                updated = apply_room_setting(base, field, payload, self._remembered.get(segment, {}))
            except ValueError as err:
                raise Refused(f"refused {room.name} {field}: {err}") from err
            if index is None:
                entries.append(updated)
            else:
                entries[index] = updated
            result = await self.robot.set_customize(entries)
            await self.refresh_rooms(await self.robot.status(), force=True)
            saved = next((e for e in self.customize or [] if e.get("segment") == segment), None)
            if saved is None or any(saved.get(k) != v for k, v in updated.items()):
                raise Refused(f"failed: {room.name} {field} -> {payload!r} not saved by the robot ({result})")
            await self._report(f"ok: {room.name} {field} -> {payload.strip()}", True)
        except Refused:
            # Snap the HA control back to what the robot really has.
            await self._publish(self.topics.rooms, rooms_payload(settings_map, self.customize,
                                                                 self._rooms_flag == settings_map.flag), retain=True)
            raise

    async def handle_consumable_reset(self, which: str) -> None:
        field = CONSUMABLE_RESET_KEYS.get(which.strip())
        if field is None:
            _LOGGER.warning("Unknown consumable %r (known: %s)", which, sorted(CONSUMABLE_RESET_KEYS))
            return
        result = await self.robot.reset_consumable(field)
        _LOGGER.info("Reset consumable %s -> %s", field, result)
        await self.refresh_info(force=True)

    async def handle_map_command(self, label: str) -> None:
        label = label.strip()
        entry = next((e for e in self.entries if e.label == label), None)
        if entry is None:
            _LOGGER.warning("Unknown map %r (known: %s)", label, [e.label for e in self.entries])
            return
        status = await self.robot.status()
        if status.current_map == entry.flag:
            _LOGGER.info("Already on map %r", label)
            return
        try:
            if is_busy(status):
                raise Refused(
                    f"refused map switch to {label}: robot is busy ({_enum_name(status.state)},"
                    f" in_cleaning={status.in_cleaning}, in_returning={status.in_returning});"
                    " let it dock or stop it first"
                )
            if status.in_returning:
                # Not moving, but a return trip is still pending (e.g. the home
                # button pressed again mid-return pauses it). The robot locks map
                # switching until that trip is cancelled. Nothing is in motion,
                # so cancelling it is safe.
                status = await self._cancel_paused_return()
            _LOGGER.info("Switching to map %r (flag %s)", label, entry.flag)
            try:
                result = await self.robot.load_map(entry.flag)
            except RobotError as err:
                if err.code != INVALID_STATUS:
                    raise
                raise Refused(
                    f"refused map switch to {label}: the robot locked it ({INVALID_STATUS} invalid status,"
                    f" state={_enum_name(status.state)}). It is probably holding an unfinished job:"
                    " press Stop on the vacuum, then pick the map again"
                ) from err
        except Refused:
            # Re-publish the real state so the select snaps back in HA.
            await self._publish(self.topics.state, state_payload(status, self.entries), retain=True)
            raise
        _LOGGER.debug("load_multi_map -> %s", result)
        await asyncio.sleep(SWITCH_SETTLE)
        status = await self.poll_once()
        if status.current_map != entry.flag:
            raise Refused(f"failed: map switch to {label}: robot still reports map {status.current_map}")
        await self._report(f"ok: map switched to {label}", True)

    async def _cancel_paused_return(self) -> StatusV2:
        """Stop a return trip that is pending but not moving, and wait for the
        robot to drop ``in_returning``. Returns the new status."""
        _LOGGER.info("A paused return trip is blocking the map switch; cancelling it")
        await self.robot.rpc("app_stop")
        deadline = time.monotonic() + STOP_SETTLE
        while True:
            await asyncio.sleep(1.0)
            status = await self.robot.status()
            if not status.in_returning:
                await self._report("ok: cancelled a paused return trip to unlock the map switch", True)
                return status
            if time.monotonic() >= deadline:
                raise Refused(
                    "refused map switch: the robot still has a return trip pending after Stop;"
                    " dock it, then pick the map again"
                )

    async def handle_delete_unnamed(self) -> None:
        """Delete every saved map that has no name, except the active one.

        A Roborock that loses its position builds a fresh, unnamed map. Those are
        the only maps this ever touches: named maps are the user's, and the
        active map is never deleted even if unnamed (switch away first).
        """
        status = await self.robot.status()
        if is_busy(status):
            _LOGGER.warning(
                "Refusing to delete maps: robot is busy (state=%s, in_cleaning=%s). Dock it first.",
                _enum_name(status.state),
                status.in_cleaning,
            )
            return
        maps = await self.robot.maps()
        unnamed = [m for m in maps.map_info or [] if not (m.name or "").strip()]
        skipped = [m.map_flag for m in unnamed if m.map_flag == status.current_map]
        targets = [m.map_flag for m in unnamed if m.map_flag != status.current_map]
        if skipped:
            _LOGGER.warning("Not deleting unnamed map %s: it is the active map. Switch away first.", skipped[0])
        if not targets:
            _LOGGER.info("No unnamed maps to delete (maps: %s)", [e.label for e in map_entries(maps)])
            return
        for flag in targets:
            result = await self.robot.delete_map(flag)
            _LOGGER.info("Deleted unnamed map %s -> %s", flag, result)
        await self.refresh_maps(force=True)
        await self.poll_once()

    # -- loops ----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        backoff = ROBOT_RECONNECT_MIN
        while True:
            try:
                await self.poll_once()
                backoff = ROBOT_RECONNECT_MIN
                if time.monotonic() < self._fast_until:
                    delay = FAST_POLL
                elif self._was_busy:
                    delay = min(BUSY_POLL, self.cfg.poll_interval)
                else:
                    delay = self.cfg.poll_interval
            except RoborockException as err:
                _LOGGER.warning("Robot unreachable: %s (retry in %.0fs)", err, backoff)
                await self._set_online(False)
                self.robot.close()
                delay, backoff = backoff, min(backoff * 2, ROBOT_RECONNECT_MAX)
            except Exception:  # noqa: BLE001 - a poll must never kill the service
                _LOGGER.exception("Unexpected error while polling")
                delay = self.cfg.poll_interval
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _command_loop(self, mqtt) -> None:
        async for message in mqtt.messages:
            payload = bytes(message.payload).decode(errors="replace")
            if message.topic.matches(self.topics.map_set):
                handler, what = self.handle_map_command(payload), "Map switch"
            elif message.topic.matches(self.topics.maps_delete_unnamed):
                handler, what = self.handle_delete_unnamed(), "Map deletion"
            elif message.topic.matches(self.topics.consumable_reset):
                handler, what = self.handle_consumable_reset(payload), "Consumable reset"
            elif message.topic.matches(self.topics.vacuum_command):
                handler, what = self.handle_vacuum_command(payload), "Vacuum command"
            elif message.topic.matches(self.topics.vacuum_fan_speed):
                handler, what = self.handle_fan_speed(payload), "Fan speed"
            elif message.topic.matches(self.topics.vacuum_clean_segments):
                handler, what = self.handle_clean_segments(payload), "Room clean"
            elif message.topic.matches(self.topics.vacuum_send_command):
                handler, what = self.handle_send_command(payload), "Send command"
            elif message.topic.matches(self.topics.room_set):
                handler, what = self.handle_room_setting(str(message.topic), payload), "Room setting"
            else:
                continue
            try:
                await handler
            except Refused as err:
                await self._report(str(err), False)
            except RoborockException as err:
                _LOGGER.warning("%s failed: %s", what, err)
                await self._report(f"failed: {what.lower()}: {err}", False)
                # Availability is the poll loop's call: one failed command (after
                # its retry) must not grey out every entity. Poll now instead.
                self._wake.set()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during %s", what.lower())

    async def run(self) -> None:
        import aiomqtt  # imported here so --dry-run works without a broker

        while True:
            try:
                async with aiomqtt.Client(
                    self.cfg.mqtt_host,
                    self.cfg.mqtt_port,
                    username=self.cfg.mqtt_username or None,
                    password=self.cfg.mqtt_password or None,
                    identifier=f"roborock_local_{self.cfg.duid}",
                    will=aiomqtt.Will(self.topics.availability, "offline", retain=True),
                    keepalive=60,
                ) as mqtt:
                    _LOGGER.info("Connected to MQTT broker %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
                    self._mqtt = mqtt
                    self._online = False
                    self._discovery_sent = None
                    await mqtt.subscribe(self.topics.map_set)
                    await mqtt.subscribe(self.topics.maps_delete_unnamed)
                    await mqtt.subscribe(self.topics.consumable_reset)
                    for topic in (self.topics.vacuum_command, self.topics.vacuum_fan_speed,
                                  self.topics.vacuum_clean_segments, self.topics.vacuum_send_command,
                                  self.topics.room_set):
                        await mqtt.subscribe(topic)
                    poller = asyncio.create_task(self._poll_loop())
                    try:
                        await self._command_loop(mqtt)
                    finally:
                        poller.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await poller
            except aiomqtt.MqttError as err:
                _LOGGER.warning("MQTT connection lost: %s (retry in %.0fs)", err, MQTT_RECONNECT)
            finally:
                self._mqtt = None
            await asyncio.sleep(MQTT_RECONNECT)

    async def dry_run(self) -> None:
        await self.refresh_maps(force=True)
        status = await self.robot.status()
        print("# discovery topic:", self.topics.discovery)
        print(json.dumps(discovery_payload(self.cfg, self.topics, self.entries, self.serial), indent=2, ensure_ascii=False))
        print("# state topic:", self.topics.state)
        print(json.dumps(state_payload(status, self.entries), indent=2))
        print("# busy:", is_busy(status))
        print("# vacuum state topic:", self.topics.vacuum_state)
        print(json.dumps(vacuum_payload(status, self.entries), indent=2, ensure_ascii=False))
        settings_map = self.settings_map
        on_map = settings_map is not None and status.current_map == settings_map.flag
        customize = await self.robot.customize() if on_map else None
        print("# rooms topic:", self.topics.rooms)
        print(json.dumps(rooms_payload(settings_map, customize, on_map), indent=2, ensure_ascii=False))
        consumable = await self.robot.consumables()
        summary = await self.robot.clean_summary()
        record = await self.robot.clean_record(summary.records[0]) if summary.records else None
        dock = await self.robot.dock_settings()
        print("# info topic:", self.topics.info)
        print(json.dumps(info_payload(consumable, summary, record, dock), indent=2))
        self.robot.close()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("roborock").setLevel(logging.WARNING)
    cfg = Config.from_env()
    bridge = Bridge(cfg)
    if "--dry-run" in sys.argv:
        asyncio.run(bridge.dry_run())
        return
    if not cfg.mqtt_host:
        sys.exit("MQTT_HOST is required (or run with --dry-run)")
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
