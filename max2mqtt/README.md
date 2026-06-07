# MAX! CUL Bridge (`max2mqtt`)

Connects ELV/eQ-3 **MAX!** radiator thermostats to **MQTT** through a
CUL/CUN stick running culfw. It receives thermostat state, lets Home Assistant
set the desired temperature, and can push weekly heating profiles to all
thermostats.

The add-on is built locally on your device (default `BUILD_FROM`, no published
image), so installs and updates go through the regular add-on store.

## Requirements

- A CUL/CUN/MapleCUL stick flashed with culfw, exposed as a serial device
  (e.g. `/dev/serial/by-id/usb-STM32_MapleCUL_...`).
- An MQTT broker (the official **Mosquitto broker** add-on works; this add-on
  declares `mqtt:need`).
- The thermostats must already be paired with the CUL base address configured
  in `cul_address` — `SetTemperature` / `ConfigWeekProfile` are only accepted
  when sent from that address.

## Hardware notes

Only one process may own the serial port at a time. If you previously ran this
as a local add-on (or FHEM), stop and remove it first so the CUL port is free.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `serial_port` | `/dev/ttyACM0` | Serial device of the CUL stick. Prefer a stable `/dev/serial/by-id/...` path. |
| `serial_baud` | `38400` | Serial baud rate (culfw default). |
| `mqtt_host` | `core-mosquitto` | MQTT broker host. |
| `mqtt_port` | `1883` | MQTT broker port. |
| `mqtt_user` | `user` | MQTT username. |
| `mqtt_password` | `"password"` | MQTT password. |
| `base_topic` | `SmartHome` | Root MQTT topic prefix. |
| `profiles_file` | `/config/heating_week_profile.yaml` | Path to the weekly-profile file. Provided via `map: config:ro` from your Home Assistant `/config`. |
| `cul_address` | `"abcdef"` | CUL base address the thermostats are paired with (lowercase hex). |
| `devices` | _(example pairs)_ | List of `room` / `address` pairs mapping each thermostat's RF address to a room key. Set these to your own thermostats. |
| `weekprofile_frame_gap` | `3.5` | Seconds between week-profile frames (duty cycle). |
| `lovf_recovery_gap` | `10.0` | Seconds to wait after a `LOVF` (duty-cycle limit) before resending. |

### Weekly profiles file

The add-on reads weekly heating profiles from `profiles_file`
(`/config/heating_week_profile.yaml` by default). That file lives in your Home
Assistant configuration directory and is **not** part of this add-on — keep it
under `/config`. See [`heating_week_profile.yaml.example`](heating_week_profile.yaml.example)
for the expected format.

## MQTT topics

Topics follow Home Assistant / MQTT conventions (snake_case, separate command and
state topics), so the climate entity needs no value templates.

Per-thermostat state under `<base_topic>/climate/<room>/...`:

- `.../current_temperature` — measured temperature
- `.../target_temperature` — set point
- `.../valve` — valve position (%)
- `.../mode` — HVAC mode (`auto`/`heat`/`off`), ready for `mode_state_topic`
- `.../battery` — `1` if battery low, else `0`
- `.../availability` — `online`

The bridge subscribes to these command topics:

- `<base_topic>/climate/<room>/target_temperature/set` — target temperature
  (e.g. `21.5`); use as the climate entity's `temperature_command_topic`
- `<base_topic>/climate/<room>/mode/set` — HVAC mode `auto`/`heat`/`off`; use as
  `mode_command_topic`, no `mode_command_template` needed (`eco`/`boost` are also
  accepted as aliases for `off`/`heat`)

Week profiles:

- `<base_topic>/weekprofile/set` — push a named profile to all thermostats
- `<base_topic>/weekprofile` — retained: the currently active profile name

### Device configuration (per room)

Each thermostat stores configuration that the add-on can write. Every setting has a
retained state topic and a `/set` command topic under `<base_topic>/climate/<room>/`,
suitable for HA `number` entities:

- `comfort_temperature`, `eco_temperature`, `max_temperature`, `min_temperature`
- `measurement_offset` — sensor calibration (−3.5…+3.5 °C)
- `window_open_temperature`, `window_open_duration` (minutes)
- `boost_duration` (one of 0/5/10/15/20/25/30/60 min), `boost_valve_position` (%)

> **These values cannot be read back from the thermostats over a CUL stick** (only the
> original eQ-3 MAX! Cube can). The add-on is therefore the source of truth: it starts
> from factory defaults, you set the real per-device values **once** in Home Assistant,
> and they are persisted in the add-on's `/data` volume across restarts. Because the
> MAX! protocol bundles them (ConfigTemperatures = all 7 temperature values, ConfigValve
> = boost duration + valve), changing one re-sends the whole group — so review the whole
> group when first configuring a device.

### Time synchronisation

The add-on answers the thermostats' `TimeInformation` requests and broadcasts the time
to all of them every `time_sync_interval_hours` (default 24), keeping their clocks — and
thus the week-program switch points — accurate.

### Rooms / device map

Each thermostat is mapped to a room via the `devices` option — a list of
`room` / `address` pairs, for example:

```yaml
devices:
  - room: LivingRoom
    address: aabb01
  - room: Bathroom
    address: aabb02
```

`address` is the thermostat's RF address (lowercase hex); `room` is a short key
used in the MQTT topics and in the weekly-profile file. The values shipped in
the add-on are placeholders — replace them with your own thermostats.

## Home Assistant configuration example

Because the topics already follow HA conventions, the climate entity needs no
value templates. One entry per thermostat (replace `SmartHome` with your
`base_topic` and `LivingRoom` with the room key):

```yaml
mqtt:
  climate:
    - name: Living Room
      current_temperature_topic: "SmartHome/climate/LivingRoom/current_temperature"
      temperature_state_topic:   "SmartHome/climate/LivingRoom/target_temperature"
      temperature_command_topic: "SmartHome/climate/LivingRoom/target_temperature/set"
      modes: ["auto", "heat", "off"]
      mode_state_topic:   "SmartHome/climate/LivingRoom/mode"
      mode_command_topic: "SmartHome/climate/LivingRoom/mode/set"
      availability_topic: "SmartHome/climate/LivingRoom/availability"
      min_temp: 5
      max_temp: 30
      temp_step: 0.5
```

A `select` entity is a convenient way to push a week profile to all thermostats
(`options` are the profile names from your `heating_week_profile.yaml`):

```yaml
mqtt:
  select:
    - name: Heating Week Profile
      command_topic: "SmartHome/weekprofile/set"
      state_topic:   "SmartHome/weekprofile"
      options: ["Comfort", "Eco"]
```

Device-configuration values map onto `number` entities, e.g. the sensor offset and
boost duration for one room:

```yaml
mqtt:
  number:
    - name: Living Room Offset
      command_topic: "SmartHome/climate/LivingRoom/measurement_offset/set"
      state_topic:   "SmartHome/climate/LivingRoom/measurement_offset"
      min: -3.5
      max: 3.5
      step: 0.5
      unit_of_measurement: "°C"
    - name: Living Room Boost Duration
      command_topic: "SmartHome/climate/LivingRoom/boost_duration/set"
      state_topic:   "SmartHome/climate/LivingRoom/boost_duration"
      min: 0
      max: 60
      step: 5
      unit_of_measurement: "min"
```

## Verifying it works

After starting the add-on, the log should show:

```
CUL opened on <port> @ 38400 baud, MAX! receiver enabled (Zr)
MQTT connected
State from ...
```

Changing the desired temperature in Home Assistant logs a
`SetTemperature → <room>: ...` line, and selecting a profile logs
`Pushing week profile '<name>' to all thermostats`.

## Protocol coverage

The add-on implements only a limited subset of the MAX! RF protocol. See
[`PROTOCOL.md`](PROTOCOL.md) for what the thermostats additionally support
(comfort/eco/offset/window settings, valve/boost tuning, timed overrides, window
sensors, grouping, wall thermostats, …) and the payload layouts for implementing
them later.
