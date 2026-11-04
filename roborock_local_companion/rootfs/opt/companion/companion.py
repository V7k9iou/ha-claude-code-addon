#!/opt/venv/bin/python
"""Roborock Local Companion.

Bridges the few things the Matter integration cannot do for a Roborock robot
-- above all switching the active saved map -- into Home Assistant, using
ONLY the robot's LAN protocol (TCP 58867) on the robot side and MQTT discovery
on the Home Assistant side. No Roborock cloud, no account token: the robot's
static ``local_key`` is all it needs.

Entities created (all under one MQTT device):
  select  Active map           -- pick a saved map; refused while the robot is busy
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

Everything else (start/stop/dock/segments/clean mode) deliberately stays on
Matter, so there is one control path per function and no second source of
truth.

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

RPC_TIMEOUT = 10.0
ROBOT_RECONNECT_MIN = 5.0
ROBOT_RECONNECT_MAX = 300.0
MQTT_RECONNECT = 10.0
SWITCH_SETTLE = 3.0
INFO_INTERVAL = 300.0  # consumables / history / dock settings refresh (also right after a job ends)

CONSUMABLE_RESET_KEYS = {
    "main_brush": "main_brush_work_time",
    "side_brush": "side_brush_work_time",
    "filter": "filter_work_time",
    "sensor": "sensor_dirty_time",
}
WASH_TOWEL_MODES = {0: "light", 1: "balanced", 2: "deep"}

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
        )


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "roborock"


# ----------------------------------------------------------------------------
# Robot: one local TCP channel, plain V1 RPC on top of it
# ----------------------------------------------------------------------------


class Robot:
    """Owns the single local connection to the robot and serialises RPCs."""

    def __init__(self, ip: str, duid: str, local_key: str) -> None:
        self._ip = ip
        self._duid = duid
        self._local_key = local_key
        self._channel: LocalChannel | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._channel is not None and self._channel.is_connected

    async def connect(self) -> None:
        if self.connected:
            return
        self.close()
        channel = LocalChannel(self._ip, self._local_key, self._duid)
        await channel.connect()
        self._channel = channel
        _LOGGER.info("Connected to %s:58867 (local protocol %s)", self._ip, channel.protocol_version.value)

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001 - closing a dead transport must never raise
                pass
            self._channel = None

    async def rpc(self, method: str, params=None):
        async with self._lock:
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
                        future.set_exception(decoded.api_error)
                    else:
                        future.set_result(decoded.data)

            unsub = await channel.subscribe(on_message)
            try:
                await channel.publish(message)
                return await asyncio.wait_for(future, RPC_TIMEOUT)
            except TimeoutError as err:
                # A silent robot usually means the TCP session is stale; force a
                # fresh connection on the next call.
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
        return await self.rpc("del_map", [flag])

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


# ----------------------------------------------------------------------------
# Map bookkeeping
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MapEntry:
    flag: int
    label: str
    rooms: int


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
        entries.append(MapEntry(flag=info.map_flag, label=label, rooms=len(info.rooms or [])))
    return entries


# ----------------------------------------------------------------------------
# MQTT payloads
# ----------------------------------------------------------------------------


class Topics:
    def __init__(self, cfg: Config) -> None:
        base = f"roborock_local/{cfg.duid}"
        self.availability = f"{base}/availability"
        self.state = f"{base}/state"
        self.map_set = f"{base}/map/set"
        self.maps_delete_unnamed = f"{base}/maps/delete_unnamed"
        self.info = f"{base}/info"
        self.consumable_reset = f"{base}/consumable/reset"
        self.discovery = f"{cfg.discovery_prefix}/device/roborock_local_{cfg.duid}/config"


def discovery_payload(cfg: Config, topics: Topics, entries: list[MapEntry], serial: str | None) -> dict:
    uid = f"roborock_local_{cfg.duid}"
    prefix = slugify(cfg.device_name)

    def entity(key: str, name: str, **extra) -> dict:
        return {
            "name": name,
            "unique_id": f"{uid}_{key}",
            "default_entity_id": f"{extra.pop('domain')}.{prefix}_{key}",
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
        "availability_topic": topics.availability,
        "state_topic": topics.state,
        "qos": 0,
        "components": {
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
        },
    }


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
        busy = is_busy(status)
        # A job just ended: the clean record and consumables changed, read them now.
        await self.refresh_info(force=self._was_busy and not busy)
        self._was_busy = busy
        await self._set_online(True)
        return status

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
        if is_busy(status):
            _LOGGER.warning(
                "Refusing to switch to %r: robot is busy (state=%s, in_cleaning=%s). Dock it first.",
                label,
                _enum_name(status.state),
                status.in_cleaning,
            )
            # Re-publish the real state so the select snaps back in HA.
            await self._publish(self.topics.state, state_payload(status, self.entries), retain=True)
            return
        _LOGGER.info("Switching to map %r (flag %s)", label, entry.flag)
        result = await self.robot.load_map(entry.flag)
        _LOGGER.debug("load_multi_map -> %s", result)
        await asyncio.sleep(SWITCH_SETTLE)
        status = await self.poll_once()
        if status.current_map != entry.flag:
            _LOGGER.warning("Robot still reports map %s after switch to %s", status.current_map, entry.flag)
        else:
            _LOGGER.info("Now on map %r", label)

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
            else:
                continue
            try:
                await handler
            except RoborockException as err:
                _LOGGER.warning("%s failed: %s", what, err)
                await self._set_online(False)
                self.robot.close()
                self._wake.set()  # let the poll loop reconnect promptly
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
