#!/usr/bin/env python3
"""MAX! CUL Bridge — connects ELV MAX! thermostats via CUL/CUN stick to MQTT."""

import json
import math
import os
import re
import serial
import threading
import logging
import time
import yaml
import paho.mqtt.client as mqtt

# Apply the timezone the Supervisor injects (TZ env) so log timestamps are in
# local time like other HAOS containers. We bypass the base image's s6 init
# (init: false + CMD), so Python must pick up TZ itself; tzdata ships with the
# base image.
try:
    time.tzset()
except AttributeError:  # non-Unix; not reached in the container
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("max2mqtt")


def _apply_timezone(configured: str = "") -> None:
    """
    Make sure local time is the Home Assistant timezone. The thermostat clocks are
    set from time.localtime(), so a container left on UTC puts the week profile
    switch points 1-2 h off. Order: `timezone` option, TZ from the environment
    (unless UTC), then the Supervisor (/supervisor/info carries "timezone").
    """
    tz = configured.strip()
    source = "option"
    if not tz and os.environ.get("TZ") and time.strftime("%z") != "+0000":
        return
    if not tz:
        source = "Supervisor"
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            try:  # s6 images keep the container env here when init is bypassed
                with open("/run/s6/container_environment/SUPERVISOR_TOKEN") as f:
                    token = f.read().strip()
            except OSError:
                pass
        if not token:
            log.warning("No timezone: TZ unset and no SUPERVISOR_TOKEN; set the "
                        "`timezone` add-on option (e.g. Europe/Berlin)")
            return
        try:
            import urllib.request
            req = urllib.request.Request("http://supervisor/supervisor/info",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                tz = json.load(r).get("data", {}).get("timezone") or ""
        except Exception as e:
            log.warning("Could not query timezone from Supervisor: %s; set the "
                        "`timezone` add-on option", e)
            return
    if tz:
        os.environ["TZ"] = tz
        time.tzset()
        log.info("Timezone set to %s (from %s)", tz, source)

# ---------------------------------------------------------------------------
# Configuration from /data/options.json (written by HAOS supervisor)
# ---------------------------------------------------------------------------
def _load_options() -> dict:
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("/data/options.json not found, using defaults")
        return {}

_opts = _load_options()
_apply_timezone(str(_opts.get("timezone", "") or ""))

SERIAL_PORT   = _opts.get("serial_port",   "/dev/ttyACM0")
SERIAL_BAUD   = int(_opts.get("serial_baud",   38400))
MQTT_HOST     = _opts.get("mqtt_host",     "core-mosquitto")
MQTT_PORT     = int(_opts.get("mqtt_port",     1883))
MQTT_USER     = _opts.get("mqtt_user",     "fhem")
MQTT_PASSWORD = _opts.get("mqtt_password", "")
BASE_TOPIC    = _opts.get("base_topic",    "SmartHome")
PROFILES_FILE = _opts.get("profiles_file", "/config/heating_week_profile.yaml")
TIME_SYNC_INTERVAL_HOURS = float(_opts.get("time_sync_interval_hours", 24))

# ---------------------------------------------------------------------------
# Device map  addr (lower) → room abbreviation used in MQTT topics.
# Configured via the `devices` add-on option (a list of room/address pairs).
# Addresses are lowercased because incoming frame addresses are lowercase hex.
# ---------------------------------------------------------------------------
DEVICES = {
    str(d["address"]).lower(): str(d["room"])
    for d in _opts.get("devices", [])
}
if not DEVICES:
    log.warning("No devices configured — set the 'devices' option (room/address pairs)")
ADDR_BY_ROOM = {v: k for k, v in DEVICES.items()}

# CUL base address the thermostats are paired with (set via the `cul_address` option).
# SetTemperature/ConfigWeekProfile MUST be sent from this address or the thermostat ignores them.
# Lowercased to match the lowercase hex of received frames.
BASE_ADDR     = _opts.get("cul_address", "abcdef").lower()

# MAX! message types
MSG_SET_TEMPERATURE     = 0x40
MSG_CONFIG_WEEK_PROF    = 0x10
MSG_CONFIG_TEMPERATURES = 0x11
MSG_CONFIG_VALVE        = 0x12
MSG_THERMOSTAT_STATE    = 0x60
MSG_TIME_INFORMATION    = 0x03
MSG_WAKEUP              = 0xF1
MSG_ACK                 = 0x02

# MAX! day order used in ConfigWeekProfile frames: Sat=0 … Fri=6
MAX_DAY_ORDER = ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]

# Boost duration (minutes) → index used in the ConfigValve frame (FHEM %boost_durations)
BOOST_DURATIONS = {0: 0, 5: 1, 10: 2, 15: 3, 20: 4, 25: 5, 30: 6, 60: 7}
# Decalcification weekday → index used in the ConfigValve frame (FHEM %decalcDays)
DECALC_DAYS = {"Sat": 0, "Sun": 1, "Mon": 2, "Tue": 3, "Wed": 4, "Thu": 5, "Fri": 6}

# Per-thermostat configuration the device stores. These cannot be read back over a
# CUL stick (only the original MAX! Cube/MAXLAN can), so the add-on is the source of
# truth: it starts from these factory defaults, is changed live over MQTT, and is
# persisted in /data. See PROTOCOL.md.
FACTORY_CONFIG = {
    "comfort_temperature":     21.0,
    "eco_temperature":         17.0,
    "max_temperature":         30.0,
    "min_temperature":          4.5,
    "measurement_offset":       0.0,
    "window_open_temperature": 12.0,
    "window_open_duration":    15,
    "boost_duration":           5,
    "boost_valve_position":    80,
    "decalcification":     "Sat 12:00",  # not exposed over MQTT
    "max_valve_setting":  100,           # not exposed over MQTT
    "valve_offset":         0,           # not exposed over MQTT
}
# Settings exposed as MQTT command/state topics (HA number entities). The three valve
# extras stay at their persisted/factory value and are written along in ConfigValve.
CONFIG_TEMPERATURE_SETTINGS = (
    "comfort_temperature", "eco_temperature", "max_temperature", "min_temperature",
    "measurement_offset", "window_open_temperature", "window_open_duration",
)
CONFIG_VALVE_SETTINGS = ("boost_duration", "boost_valve_position")
CONFIG_SETTINGS = CONFIG_TEMPERATURE_SETTINGS + CONFIG_VALVE_SETTINGS

# Outgoing message counter (incremented per frame; thermostats dedupe on a fixed counter)
_msg_counter      = 0
_msg_counter_lock = threading.Lock()


def next_counter() -> int:
    global _msg_counter
    with _msg_counter_lock:
        _msg_counter = (_msg_counter + 1) & 0xFF
        return _msg_counter

# ---------------------------------------------------------------------------
# Serial / CUL handling
# ---------------------------------------------------------------------------
_serial: serial.Serial | None = None
_serial_lock = threading.Lock()


def open_serial() -> serial.Serial:
    s = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
    # Init CUL: X21 = report mode with RSSI, Zr = enable Moritz/MAX! receiver.
    # Without Zr the culfw firmware does not process incoming MAX! frames.
    s.write(b"X21\r\n")
    time.sleep(0.2)
    s.write(b"Zr\r\n")
    time.sleep(0.2)
    s.reset_input_buffer()
    log.info("CUL opened on %s @ %d baud, MAX! receiver enabled (Zr)", SERIAL_PORT, SERIAL_BAUD)
    return s


def cul_send(frame_hex: str) -> bool:
    """
    Send a raw hex frame via the CUL stick (Zs = MAX! send with wakeup preamble).

    Returns True on success, False on failure. Never raises — a serial error
    here must not crash the MQTT loop. On failure the port is closed and reset
    so the reader thread reconnects.
    """
    global _serial
    with _serial_lock:
        s = _serial
        if s is None:
            log.warning("CUL not connected, dropping frame %s", frame_hex)
            return False
        try:
            s.write(f"Zs{frame_hex}\r\n".encode())
            log.debug("CUL TX: %s", frame_hex)
            return True
        except (serial.SerialException, OSError) as e:
            log.error("CUL write failed: %s", e)
            try:
                s.close()
            except Exception:
                pass
            _serial = None  # signal reader loop to reopen the port
            return False


# ---------------------------------------------------------------------------
# MAX! frame builder/parser
# ---------------------------------------------------------------------------

def build_frame(msg_type: int, src: str, dst: str, payload: bytes, group_id: int = 0, flags: int | None = None) -> str:
    """
    Build a MAX! RF frame and return it as a hex string ready for Zs<hex>.

    Frame layout (big-endian):
      length(1) counter(1) flags(1) type(1) src(3) dst(3) group(1) payload(N)

    Flags default to 0x00 for addressed messages and 0x04 for broadcasts
    (dst 000000), matching FHEM's CUL_MAX_Send behaviour. The message counter
    is incremented per frame so the thermostat does not treat repeats as dups.
    """
    if flags is None:
        flags = 0x04 if dst == "000000" else 0x00
    src_bytes  = bytes.fromhex(src)
    dst_bytes  = bytes.fromhex(dst)
    counter    = next_counter()
    header     = bytes([counter, flags, msg_type]) + src_bytes + dst_bytes + bytes([group_id])
    frame      = bytes([len(header) + len(payload)]) + header + payload
    return frame.hex().upper()


def encode_set_temperature(temp: float, mode: int = 0) -> bytes:
    """
    Encode a SetTemperature (0x40) payload byte.
    mode bits: 0=auto, 1=manual, 2=vacation, 3=boost
    temp is encoded as int(temp*2) in lower 6 bits, mode in upper 2 bits.
    """
    t_enc = int(temp * 2) & 0x3F
    return bytes([(mode << 6) | t_enc])


def _enc_temp(t: float) -> int:
    """Encode a temperature as int(t*2), clamped to the valid 0…30.5 °C byte range."""
    return max(0, min(0xFF, int(round(t * 2))))


def encode_time_information() -> bytes:
    """
    Encode a TimeInformation (0x03) payload from the local time (5 bytes).
    Layout (FHEM CUL_MAX_GetTimeInformationPayload), month split across two bytes:
      year-2000, day, hour, min|((mon&0x0C)<<4), sec|((mon&0x03)<<6)
    """
    t = time.localtime()
    mon = t.tm_mon  # 1-based
    return bytes([
        t.tm_year - 2000,
        t.tm_mday,
        t.tm_hour,
        t.tm_min | ((mon & 0x0C) << 4),
        t.tm_sec | ((mon & 0x03) << 6),
    ])


def encode_config_temperatures(cfg: dict) -> bytes:
    """
    Encode a ConfigTemperatures (0x11) payload (7 bytes):
      comfort, eco, max, min, offset, windowOpenTemp, windowOpenDuration
    Temperatures are int(t*2); offset is int((offset+3.5)*2); duration is minutes/5.
    """
    offset = max(0, min(0xFF, int(round((cfg["measurement_offset"] + 3.5) * 2))))
    win_dur = max(0, min(0xFF, int(round(cfg["window_open_duration"] / 5))))
    return bytes([
        _enc_temp(cfg["comfort_temperature"]),
        _enc_temp(cfg["eco_temperature"]),
        _enc_temp(cfg["max_temperature"]),
        _enc_temp(cfg["min_temperature"]),
        offset,
        _enc_temp(cfg["window_open_temperature"]),
        win_dur,
    ])


def encode_config_valve(cfg: dict) -> bytes:
    """
    Encode a ConfigValve (0x12) payload (4 bytes): boost, decalc, maxValve, valveOffset.
      boost  = (boostDuration_index << 5) | int(boostValvePosition / 5)
      decalc = (decalcDay << 5) | decalcHour
    """
    dur_idx = BOOST_DURATIONS.get(int(cfg["boost_duration"]), 1)  # default 5 min
    boost = (dur_idx << 5) | (int(cfg["boost_valve_position"]) // 5 & 0x1F)
    day_str, _, hhmm = str(cfg["decalcification"]).partition(" ")
    decalc = (DECALC_DAYS.get(day_str, 0) << 5) | (int(hhmm.split(":")[0]) if ":" in hhmm else 12)
    max_valve = max(0, min(0xFF, int(round(int(cfg["max_valve_setting"]) * 255 / 100))))
    valve_off = max(0, min(0xFF, int(round(int(cfg["valve_offset"]) * 255 / 100))))
    return bytes([boost & 0xFF, decalc & 0xFF, max_valve, valve_off])


def parse_thermostat_state(payload_hex: str) -> dict:
    """
    Parse a ThermostatState (0x60) frame payload.
    Returns a dict with keys: mode, valve, desired_temp, actual_temp, battery_low.
    "mode" is the HA HVAC mode (auto/heat/off) ready for the climate entity.
    """
    data = bytes.fromhex(payload_hex)
    if len(data) < 2:
        return {}

    flags        = data[0]
    valve        = data[1]
    battery_low  = bool(flags & 0x80)
    # mode is bits 1-0 of flags byte in state response
    mode_raw     = flags & 0x03
    mode_names   = {0: "auto", 1: "manual", 2: "vacation", 3: "boost"}
    max_mode     = mode_names.get(mode_raw, "auto")

    desired_temp = None
    actual_temp  = None

    if len(data) >= 3:
        desired_temp = (data[2] & 0x7F) / 2.0

    if len(data) >= 5:
        # Actual temp is encoded across bytes 3 and 4
        actual_temp = ((data[3] & 0x01) << 8 | data[4]) / 10.0

    # Map MAX! mode → HA HVAC mode (auto/heat/off) for the climate entity
    if max_mode == "boost":
        hvac_mode = "heat"
    elif max_mode == "manual" and desired_temp is not None and desired_temp <= 17.0:
        hvac_mode = "off"
    else:
        hvac_mode = "auto"

    return {
        "mode":        hvac_mode,
        "valve":       valve,
        "desired_temp": desired_temp,
        "actual_temp": actual_temp,
        "battery_low": battery_low,
    }


# ---------------------------------------------------------------------------
# Week profile encoding
# ---------------------------------------------------------------------------

def _encode_slot(end_time_str: str, temp: float) -> bytes:
    """Encode one 2-byte MAX! week-profile slot."""
    h, m    = map(int, end_time_str.split(":"))
    if h == 24:
        h = 24; m = 0
    end_minutes = h * 60 + m
    t_enc       = int(temp * 2)
    time_enc    = end_minutes // 5
    byte1 = ((t_enc << 1) | (time_enc >> 8)) & 0xFF
    byte2 = time_enc & 0xFF
    return bytes([byte1, byte2])


def encode_day_slots(slots: list) -> bytes:
    """
    Encode up to 13 slots for one day.
    The last slot must have end_time "24:00"; extra slots are padded by repeating it.
    Returns 26 bytes (13 × 2).
    """
    SLOT_COUNT = 13
    encoded = bytearray()
    for end_time, temp in slots:
        encoded += _encode_slot(end_time, temp)

    # Pad to 13 slots by repeating the last slot
    while len(encoded) < SLOT_COUNT * 2:
        encoded += encoded[-2:]

    return bytes(encoded[:SLOT_COUNT * 2])


def build_week_profile_frames(room: str, day_schedules: dict) -> list[str]:
    """
    Build ConfigWeekProfile (0x10) frames for all 7 days of a room.

    day_schedules: dict of {day_name: [(end_time, temp), ...]} where day_name in
                  MAX_DAY_ORDER or "default".  Missing days fall back to "default".

    A MAX! ConfigWeekProfile telegram carries at most 7 control points, so each
    day is sent as up to TWO telegrams (matching FHEM 10_MAX.pm):
      payload = <header byte> + control points (2 bytes each)
      header  = (half << 4) | day_index   half 0 = control points 0..6 (7),
                                          half 1 = control points 7..12 (6)
      day_index: 0=Sat … 6=Fri (MAX_DAY_ORDER)
    A single 13-point/52-byte frame exceeds the culfw Moritz buffer (-> LENERR).

    The upper-half telegram is only sent when a day actually has more than 7
    control points. With ≤7 points the schedule already ends with its 24:00
    terminator inside the lower half, so the day is complete and the extra
    (all-padding) telegram is skipped — halving the airtime for typical profiles
    and staying within the 1 % duty-cycle budget.
    """
    dst = ADDR_BY_ROOM[room]
    frames = []

    default_slots = day_schedules.get("default", [["24:00", 17.0]])

    for day_idx, day in enumerate(MAX_DAY_ORDER):
        slots     = day_schedules.get(day, default_slots)
        day_bytes = encode_day_slots(slots)          # 13 control points = 26 bytes

        # Telegram 1: control points 0..6 (lower half)
        payload_lo = bytes([(0 << 4) | day_idx]) + day_bytes[0:14]
        frames.append(build_frame(MSG_CONFIG_WEEK_PROF, BASE_ADDR, dst, payload_lo))

        # Telegram 2: control points 7..12 (upper half) — only when really needed
        if len(slots) > 7:
            payload_hi = bytes([(1 << 4) | day_idx]) + day_bytes[14:26]
            frames.append(build_frame(MSG_CONFIG_WEEK_PROF, BASE_ADDR, dst, payload_hi))

    return frames


# ---------------------------------------------------------------------------
# MQTT handling
# ---------------------------------------------------------------------------
_mqtt_client: mqtt.Client | None = None

# Last known desired setpoint per room. A MAX! boost is valve/duration based and
# should not move the target, so a boost command re-sends this value instead of a
# fixed temperature. Fed from thermostat state echoes and explicit set commands.
_last_desired: dict[str, float] = {}

# Per-room thermostat configuration (see FACTORY_CONFIG). Source of truth in the
# add-on; persisted to /data because the values cannot be read back over the CUL.
CONFIG_PATH = "/data/config_state.json"
_config: dict[str, dict] = {}
_config_lock = threading.Lock()


def _load_config() -> None:
    """Initialise _config from factory defaults, overlaid with persisted /data values."""
    persisted = {}
    try:
        with open(CONFIG_PATH) as f:
            persisted = json.load(f)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read %s (%s); using factory defaults", CONFIG_PATH, e)
    for room in ADDR_BY_ROOM:
        merged = dict(FACTORY_CONFIG)
        merged.update(persisted.get(room, {}))
        _config[room] = merged


def _save_config() -> None:
    """Persist _config atomically to /data."""
    try:
        tmp = CONFIG_PATH + ".tmp"
        with _config_lock, open(tmp, "w") as f:
            json.dump(_config, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log.error("Could not persist config to %s: %s", CONFIG_PATH, e)


def mqtt_publish(topic: str, payload: str, retain: bool = False) -> None:
    _mqtt_client.publish(topic, payload, retain=retain)
    log.debug("MQTT PUB %s = %s", topic, payload)


def publish_thermostat_state(room: str, state: dict) -> None:
    # Retain state so Home Assistant restores the last known values after a
    # restart, instead of showing an empty entity until the next RF report.
    prefix = f"{BASE_TOPIC}/climate/{room}"
    if state.get("actual_temp") is not None:
        mqtt_publish(f"{prefix}/current_temperature", str(state["actual_temp"]), retain=True)
    if state.get("desired_temp") is not None:
        mqtt_publish(f"{prefix}/target_temperature", str(state["desired_temp"]), retain=True)
        # Remember the real setpoint for boost preservation, but skip boost echoes
        # (mode "heat") so we don't capture the transient boost value.
        if state.get("mode") != "heat":
            _last_desired[room] = state["desired_temp"]
    if state.get("valve") is not None:
        mqtt_publish(f"{prefix}/valve",              str(state["valve"]), retain=True)
    mqtt_publish(f"{prefix}/mode",         state.get("mode", "auto"), retain=True)
    mqtt_publish(f"{prefix}/battery",      "1" if state.get("battery_low") else "0", retain=True)
    mqtt_publish(f"{prefix}/availability", "online", retain=True)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("MQTT connected")
        # MQTT '+' must be a whole level, so subscribe per room.
        for room in ADDR_BY_ROOM:
            client.subscribe(f"{BASE_TOPIC}/climate/{room}/target_temperature/set")
            client.subscribe(f"{BASE_TOPIC}/climate/{room}/mode/set")
            for setting in CONFIG_SETTINGS:
                client.subscribe(f"{BASE_TOPIC}/climate/{room}/{setting}/set")
            publish_config_state(room)
        client.subscribe(f"{BASE_TOPIC}/weekprofile/set")
    else:
        log.error("MQTT connect failed, rc=%d", rc)


def on_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode().strip()
    log.info("MQTT RX %s = %s", topic, payload)

    # ---- Per-room climate command topics (shared parser dispatches on setting) ----
    m = re.match(rf"^{re.escape(BASE_TOPIC)}/climate/(\w+)/(\w+)/set$", topic)
    if m:
        room, setting = m.group(1), m.group(2)
        if room not in ADDR_BY_ROOM:
            log.warning("Unknown room: %s", room)
            return
        if setting in ("target_temperature", "mode"):
            _handle_set_temperature(room, payload)
        elif setting in CONFIG_SETTINGS:
            _handle_set_config(room, setting, payload)
        else:
            log.warning("Unknown setting %s for room %s", setting, room)
        return

    # ---- Week profile push ----
    if topic == f"{BASE_TOPIC}/weekprofile/set":
        _handle_set_week_profile(payload)
        return


def _push_setpoint(room: str, mode: int, temp: float, resume_profile: bool, is_current) -> None:
    frame = build_frame(MSG_SET_TEMPERATURE, BASE_ADDR, ADDR_BY_ROOM[room],
                        encode_set_temperature(temp, mode))
    log.info("SetTemperature → %s: mode=%d temp=%.1f%s", room, mode, temp,
             " (resume week profile)" if resume_profile else "")
    result, _ = _deliver(room, lambda: (frame, None), f"settemp {room}", is_current)
    if result != "superseded":
        _publish_status(room, "target_temperature", "ok" if result == "ok" else "failed")
        if result != "ok":
            log.warning("SetTemperature for %s was not delivered", room)


def _handle_set_temperature(room: str, payload: str) -> None:
    """
    Payload from HA can be:
      - "auto"           → mode=auto, temp byte 0 (resume week profile, no override)
      - "off"  / "eco"   → mode=manual, temp=17.0 (off equivalent)
      - "heat" / "boost" → mode=boost, keeps the current setpoint
      - "auto <temp>"    → mode=auto, explicit temp
      - "<float>"        → mode=manual, explicit temp

    The HVAC modes "auto"/"heat"/"off" are accepted directly, so the MQTT climate
    entity can publish its raw mode here without a mode_command_template mapping.
    "eco"/"boost" remain accepted as aliases for backward compatibility.
    """
    payload = payload.strip()
    mode     = 1   # manual
    temp     = 17.0

    resume_profile = False
    if payload == "auto":
        # Auto with temperature byte 0 means "no override": the thermostat returns
        # to its week profile (FHEM: `desiredTemperature auto`). Any non-zero value
        # is held as a temporary override until the next profile switch point — a
        # fixed 17.0 dropped rooms to Eco, re-sending the last setpoint (1.0.2)
        # pinned them at the old manual value for hours.
        mode = 0; temp = 0.0
        resume_profile = True
    elif payload in ("off", "eco"):
        mode = 1; temp = 17.0
    elif payload in ("heat", "boost"):
        # Boost intensity/duration is configured on the thermostat itself; re-send
        # the current setpoint so boosting doesn't move the displayed target.
        mode = 3; temp = _last_desired.get(room, 22.0)
    elif payload == "auto comfort":
        mode = 0; temp = 21.0
    elif payload.startswith("auto "):
        mode = 0
        try:
            temp = float(payload.split(None, 1)[1])
        except ValueError:
            temp = 17.0
    else:
        try:
            temp = float(payload)
        except ValueError:
            log.warning("Cannot parse temperature payload: %s", payload)
            return

    # Latest command per room wins; sent paced, serialised and ack-checked.
    _publish_status(room, "target_temperature", "pending")
    _schedule(("setpoint", room), DEBOUNCE_SETPOINT, _push_setpoint,
              room, mode, temp, resume_profile)

    # Seed the boost-preservation cache from explicit setpoints (not from boost
    # itself, nor from the 17.0 off/auto placeholders).
    if mode != 3 and temp > 17.0:
        _last_desired[room] = temp

    # Optimistically reflect the change in HA immediately; the thermostat's own
    # state echo can take a minute or more to arrive over RF.
    hvac_mode  = "heat" if mode == 3 else ("off" if (mode == 1 and temp <= 17.0) else "auto")
    prefix     = f"{BASE_TOPIC}/climate/{room}"
    if not resume_profile:  # the profile's setpoint is unknown until the thermostat reports
        mqtt_publish(f"{prefix}/target_temperature", str(temp), retain=True)
    mqtt_publish(f"{prefix}/mode", hvac_mode, retain=True)


# Duty-cycle aware sending.
# The 868.3 MHz SRD band allows a 1 % duty cycle. culfw tracks this as a credit
# (credit10ms) that regenerates at ~1 unit per second; a send costs
#   ceil(100 + hexlen*4/10)  units   (FHEM 14_CUL_MAX.pm)
# dominated by the 100-unit (1 s) wakeup preamble. "Fast send" (Zf, no preamble)
# would be ~6x cheaper, but these heating thermostats have no listen window
# after their ack (they only stay awake after an explicit WakeUp message), so a
# Zf is never received here — it just wastes time and credit. We therefore
# always send with preamble (Zs). When the budget is empty culfw answers "LOVF"
# and drops the frame; we wait the frame's cost in seconds (≈ credit needed)
# and resend it. Delivery is confirmed by the thermostat's ack.
LOVF_DETECT_WINDOW = 0.6   # seconds to catch a LOVF reply after a send
ACK_TIMEOUT        = 2.0   # seconds to wait for the thermostat's ack
MAX_FRAME_RETRIES  = 8     # per frame before giving up
# Minimum gap between two consecutive preamble sends (Zs). A Zs blocks the stick
# for ~1 s (preamble) + frame; queuing a second one right behind it makes the
# MapleCUL drop off the USB bus ~2.06 s after the first write. Observed with
# WakeUp+config (0.5 s gap) and with the time broadcast (1 s gap); stable at 2 s.
TX_SETTLE          = 2.0
# Kept for add-on config compatibility (no longer used directly):
WEEKPROFILE_FRAME_GAP = float(_opts.get("weekprofile_frame_gap", 3.5))
LOVF_RECOVERY_GAP     = float(_opts.get("lovf_recovery_gap", 10.0))

_lovf_event = threading.Event()
_ack_events = {addr: threading.Event() for addr in DEVICES}

# Serialises all multi-frame / ack-based transmissions (week profile, config pushes,
# time broadcast). The send path assumes a single sender at a time (_lovf_event is
# global, _ack_events are per device), so concurrent senders must not overlap.
_tx_lock = threading.Lock()


def _wake(room: str) -> None:
    """Best-effort WakeUp (0xF1) so the thermostat is awake for the following frame(s)."""
    frame = build_frame(MSG_WAKEUP, BASE_ADDR, ADDR_BY_ROOM[room], b"\x3f")
    if _paced_send(frame):
        log.info("WakeUp → %s", room)


def _frame_credit_cost(frame_hex: str) -> int:
    """credit10ms cost of a preamble send; with ~1 unit/s regen this also equals the seconds to wait."""
    return math.ceil(100 + len(frame_hex) * 4 / 10)


_last_tx = 0.0  # monotonic time of the last preamble send (only touched under _tx_lock)


def _paced_send(frame: str) -> bool:
    """cul_send, but never closer than TX_SETTLE to the previous preamble send."""
    global _last_tx
    wait = _last_tx + TX_SETTLE - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    ok = cul_send(frame)
    _last_tx = time.monotonic()
    return ok


# Debounce / coalesce rapid commands (HA sliders, repeated clicks): only the newest
# command per key is sent, after a short quiet period.
DEBOUNCE_SETPOINT = 1.0   # seconds
DEBOUNCE_CONFIG   = 2.0   # seconds
RETRY_DELAYS      = (10, 30, 60)  # pauses before re-trying a failed delivery

_sched_gen: dict = {}
_sched_lock = threading.Lock()


def _schedule(key, delay: float, fn, *args) -> None:
    """Run fn(*args, is_current) in a thread after `delay`; a newer _schedule(key) supersedes it."""
    with _sched_lock:
        gen = _sched_gen.get(key, 0) + 1
        _sched_gen[key] = gen

    def is_current() -> bool:
        with _sched_lock:
            return _sched_gen.get(key) == gen

    def run() -> None:
        time.sleep(delay)
        if is_current():
            fn(*args, is_current)

    threading.Thread(target=run, daemon=True, name=f"tx-{key}").start()


def _deliver(room: str, build, label: str, is_current, wake: bool = False,
             frame_retries: int = 2):
    """
    Send a frame (rebuilt via build() -> (frame, token) on every attempt), waiting for
    the ack, and retry after RETRY_DELAYS if it was not delivered (e.g. stick reconnect).
    Returns (result, token): result is "ok", "failed", or "superseded" (a newer
    command replaced this one while retrying).
    """
    token = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        if attempt:
            log.info("%s not delivered, retry %d/%d in %ds", label, attempt,
                     len(RETRY_DELAYS), RETRY_DELAYS[attempt - 1])
            time.sleep(RETRY_DELAYS[attempt - 1])
        if not is_current():
            return "superseded", token
        frame, token = build()
        with _tx_lock:
            if wake:
                _wake(room)
            if _send_frame(room, frame, label, frame_retries) == "ok":
                return "ok", token
    return "failed", token


def _publish_status(room: str, setting: str, status: str) -> None:
    """Delivery status of a command: pending → ok | failed (retained)."""
    mqtt_publish(f"{BASE_TOPIC}/climate/{room}/{setting}/status", status, retain=True)


def _handle_set_week_profile(profile_name: str) -> None:
    """Push the week profile in a background thread so the MQTT loop is never blocked."""
    threading.Thread(
        target=_push_week_profile, args=(profile_name,), daemon=True,
        name=f"weekprofile-{profile_name}",
    ).start()


def _send_frame(room: str, frame: str, label: str, retries: int = MAX_FRAME_RETRIES) -> str:
    """
    Send one frame to a thermostat (with preamble) and wait for its ack.

    On LOVF, waits the frame's credit cost in seconds and resends. Confirms
    delivery via the thermostat's ack; resends on no ack. Returns one of:
      "ok"           — acked
      "serial_error" — CUL write failed (port lost)
      "giveup"       — not delivered after MAX_FRAME_RETRIES
    """
    ack  = _ack_events[ADDR_BY_ROOM[room]]
    cost = _frame_credit_cost(frame)

    for attempt in range(retries + 1):
        ack.clear()
        _lovf_event.clear()
        if not _paced_send(frame):
            return "serial_error"
        log.info("TX %s%s", label, "" if attempt == 0 else f" retry {attempt}")

        # culfw replies LOVF almost immediately if the budget is exhausted.
        if _lovf_event.wait(LOVF_DETECT_WINDOW):
            log.info("LOVF %s — waiting %ds for duty-cycle credit", label, cost)
            time.sleep(cost)
            continue

        if ack.wait(ACK_TIMEOUT):
            return "ok"
        log.info("no ack for %s, resending", label)

    return "giveup"


def _push_week_profile(profile_name: str) -> None:
    """Load profile from YAML and push ConfigWeekProfile frames to all thermostats (paced)."""
    try:
        with open(PROFILES_FILE) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        log.error("Cannot read profiles file %s: %s", PROFILES_FILE, e)
        return

    profiles = data.get("profiles", {})
    if profile_name not in profiles:
        log.warning("Profile not found: %s (available: %s)", profile_name, list(profiles.keys()))
        return

    profile = profiles[profile_name]
    log.info("Pushing week profile '%s' to all thermostats", profile_name)

    with _tx_lock:
        for room in ADDR_BY_ROOM:
            if room not in profile:
                log.warning("Room %s not in profile %s, skipping", room, profile_name)
                continue
            frames = build_week_profile_frames(room, profile[room])
            sent = 0
            for idx, frame in enumerate(frames, 1):
                result = _send_frame(room, frame, f"weekprofile {room} {idx}/{len(frames)}")
                if result == "serial_error":
                    log.warning("CUL unavailable, aborting week profile push for %s after %d/%d frames",
                                room, sent, len(frames))
                    return
                if result == "giveup":
                    log.error("Frame %s %d/%d could not be delivered, aborting push",
                              room, idx, len(frames))
                    return
                sent += 1
            log.info("Week profile pushed to %s (%d frames)", room, sent)

    # Confirm active profile back to HA (retained state for the weekprofile topic)
    mqtt_publish(f"{BASE_TOPIC}/weekprofile", profile_name, retain=True)
    log.info("Week profile '%s' activated", profile_name)


# ---------------------------------------------------------------------------
# Thermostat configuration (ConfigTemperatures / ConfigValve)
# ---------------------------------------------------------------------------
_INT_CONFIG_SETTINGS = {"window_open_duration", "boost_duration", "boost_valve_position"}


def _coerce_config(setting: str, raw: str):
    """Parse and clamp an MQTT config value; return the cleaned value or None if invalid."""
    try:
        num = float(raw)
    except ValueError:
        return None
    if setting == "measurement_offset":
        num = max(-3.5, min(3.5, round(num * 2) / 2))
    elif setting in ("comfort_temperature", "eco_temperature", "max_temperature",
                     "min_temperature", "window_open_temperature"):
        num = max(4.5, min(30.5, round(num * 2) / 2))
    elif setting == "window_open_duration":
        num = max(0, min(60, int(round(num / 5) * 5)))
    elif setting == "boost_valve_position":
        num = max(0, min(100, int(round(num / 5) * 5)))
    elif setting == "boost_duration":
        num = min(BOOST_DURATIONS, key=lambda d: abs(d - num))  # snap to a valid duration
    return int(num) if setting in _INT_CONFIG_SETTINGS else float(num)


_pending_config: dict = {}  # (room, group) -> settings changed but not yet acked
_pending_config_lock = threading.Lock()


def _handle_set_config(room: str, setting: str, payload: str) -> None:
    """Validate a config setting, persist it, echo retained state, push (debounced) in background."""
    value = _coerce_config(setting, payload.strip())
    if value is None:
        log.warning("Cannot parse config %s for %s: %r", setting, room, payload)
        return
    _config[room][setting] = value
    _save_config()
    mqtt_publish(f"{BASE_TOPIC}/climate/{room}/{setting}", str(value), retain=True)
    group = "valve" if setting in CONFIG_VALVE_SETTINGS else "temp"
    with _pending_config_lock:
        _pending_config.setdefault((room, group), set()).add(setting)
    _publish_status(room, setting, "pending")
    # The frame carries the whole group, so several quick changes go out as one.
    _schedule(("config", room, group), DEBOUNCE_CONFIG, _push_config, room, group)


def _push_config(room: str, group: str, is_current) -> None:
    """WakeUp + send the full ConfigTemperatures or ConfigValve frame for a room."""
    def build():
        cfg = dict(_config[room])
        with _pending_config_lock:
            snapshot = set(_pending_config.get((room, group), ()))
        if group == "valve":
            frame = build_frame(MSG_CONFIG_VALVE, BASE_ADDR, ADDR_BY_ROOM[room], encode_config_valve(cfg))
        else:
            frame = build_frame(MSG_CONFIG_TEMPERATURES, BASE_ADDR, ADDR_BY_ROOM[room],
                                encode_config_temperatures(cfg))
        return frame, snapshot

    label = f"config{'valve' if group == 'valve' else 'temp'} {room}"
    result, snapshot = _deliver(room, build, label, is_current, wake=True)
    if result == "superseded":
        return  # the newer push carries these settings too
    with _pending_config_lock:
        _pending_config.get((room, group), set()).difference_update(snapshot or ())
    for setting in snapshot or ():
        _publish_status(room, setting, "ok" if result == "ok" else "failed")
    if result == "ok":
        log.info("Config applied to %s: %s", room, ", ".join(sorted(snapshot or ())))
    else:
        log.warning("Config push to %s (%s) failed after %d retries", room, group, len(RETRY_DELAYS))


def publish_config_state(room: str) -> None:
    """Publish all MQTT-exposed config settings of a room as retained state."""
    for setting in CONFIG_SETTINGS:
        mqtt_publish(f"{BASE_TOPIC}/climate/{room}/{setting}", str(_config[room][setting]), retain=True)


# ---------------------------------------------------------------------------
# CUL receive loop
# ---------------------------------------------------------------------------

def parse_cul_line(line: str) -> None:
    """Parse one line received from the CUL stick."""
    line = line.strip()
    if not line:
        return
    log.debug("CUL RX raw: %s", line)
    if line == "LOVF":
        # Duty-cycle budget exhausted; the last send was not transmitted.
        _lovf_event.set()
        return
    if not line.startswith("Z"):
        return
    if len(line) < 3:
        return

    # Format: Z<len_hex><frame_hex>
    try:
        frame_hex = line[1:]   # drop the leading 'Z'
        frame     = bytes.fromhex(frame_hex)
    except ValueError:
        log.debug("Unparseable CUL line: %s", line)
        return

    if len(frame) < 11:
        return

    # Frame: length(1) counter(1) flags(1) type(1) src(3) dst(3) group(1) payload(…)
    msg_type    = frame[3]
    src_addr    = frame[4:7].hex()
    dst_addr    = frame[7:10].hex()
    payload_hex = frame[11:].hex()

    room_hint = DEVICES.get(src_addr, "?")
    log.debug("RX type=0x%02x src=%s(%s) dst=%s payload=%s",
              msg_type, src_addr, room_hint, dst_addr, payload_hex)

    # Ack of one of our commands (e.g. ConfigWeekProfile) addressed to us.
    if msg_type == MSG_ACK and dst_addr == BASE_ADDR and src_addr in _ack_events:
        _ack_events[src_addr].set()

    # A thermostat asking us for the time — answer so its clock (and therefore the
    # week program switch points) stays accurate. Skip if a send is in progress.
    if msg_type == MSG_TIME_INFORMATION and dst_addr == BASE_ADDR:
        if _tx_lock.acquire(blocking=False):
            try:
                cul_send(build_frame(MSG_TIME_INFORMATION, BASE_ADDR, src_addr,
                                     encode_time_information(), flags=0x04))
                log.info("TimeInformation request from %s — sent", room_hint)
            finally:
                _tx_lock.release()
        return

    if msg_type == MSG_THERMOSTAT_STATE:
        room = DEVICES.get(src_addr)
        if room:
            state = parse_thermostat_state(payload_hex)
            if state:
                publish_thermostat_state(room, state)
                log.info("State from %s: %s", room, state)
        else:
            log.warning("ThermostatState from unknown device: %s", src_addr)


def serial_reader_loop() -> None:
    global _serial
    while True:
        s = None
        try:
            s = open_serial()
            with _serial_lock:
                _serial = s
            while True:
                line = s.readline().decode(errors="replace")
                if line:
                    parse_cul_line(line)
        except (serial.SerialException, OSError) as e:
            log.error("Serial error: %s — retrying in 5s", e)
        except Exception as e:
            log.error("Unexpected serial error: %s — retrying in 5s", e)
        finally:
            with _serial_lock:
                _serial = None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        time.sleep(5)


def time_broadcast_loop() -> None:
    """Periodically send the current time to every thermostat so its clock stays accurate."""
    interval = max(1.0, TIME_SYNC_INTERVAL_HOURS) * 3600
    time.sleep(30)  # let the serial port and MQTT settle after startup
    while True:
        with _tx_lock:
            for room in ADDR_BY_ROOM:
                frame = build_frame(MSG_TIME_INFORMATION, BASE_ADDR, ADDR_BY_ROOM[room],
                                    encode_time_information(), flags=0x04)
                if not _paced_send(frame):
                    log.warning("TimeInformation broadcast aborted at %s (CUL unavailable)", room)
                    break
                log.info("TimeInformation broadcast → %s (%s %s)", room,
                         time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%Z %z"))
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _mqtt_client

    _load_config()
    log.info("Timezone: TZ=%r, local time %s %s (thermostat clocks are set from this)",
             os.environ.get("TZ"), time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%Z %z"))

    _mqtt_client = mqtt.Client()
    if MQTT_USER:
        _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    _mqtt_client.on_connect = on_connect
    _mqtt_client.on_message = on_message
    _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    reader = threading.Thread(target=serial_reader_loop, daemon=True)
    reader.start()

    threading.Thread(target=time_broadcast_loop, daemon=True, name="time-broadcast").start()

    log.info("max2mqtt started — serial=%s mqtt=%s:%d", SERIAL_PORT, MQTT_HOST, MQTT_PORT)
    _mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
