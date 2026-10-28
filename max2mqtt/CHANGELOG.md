# Changelog

## 1.0.8

- Thermostat Acks now update valve, mode and setpoint (the Ack carries the state
  bytes without the measured temperature). Previously the valve only refreshed on
  the thermostat's own state broadcasts, which can be hours apart, so it could
  show a stale value long after the last command.

## 1.0.7

- Setpoint, mode and boost commands now go through the same serialised, paced and
  ack-checked send path as config pushes (previously fire-and-forget and able to
  collide with other transmissions or trigger the stick disconnect).
- Rapid commands are debounced and coalesced (newest wins): setpoint 1 s, config 2 s;
  several config changes of one group go out as a single frame.
- Failed deliveries are retried after 10/30/60 s (also across a stick reconnect),
  unless superseded by a newer command.
- New retained status topics `<base_topic>/climate/<room>/<setting>/status`
  (`pending` / `ok` / `failed`), see README.
- All preamble sends are spaced at least `TX_SETTLE` apart, regardless of origin.

## 1.0.6

- Fixed: the Supervisor timezone lookup was rejected with HTTP 403. The add-on now
  requests `hassio_api` (read-only use: `/supervisor/info`), so the timezone is
  taken from Home Assistant without setting the `timezone` option.

## 1.0.5

- Fixed: thermostat clocks (and with them the week profile switch points) ran in
  UTC because the container gets no `TZ`. The add-on now asks the Supervisor for
  the Home Assistant timezone when `TZ` is missing or UTC, and has a new
  `timezone` option (e.g. `Europe/Berlin`) as an explicit override. The effective
  timezone is logged at startup and with every time broadcast.

## 1.0.4

- Fixed: switching back to `auto` (mode or `target_temperature/set = auto`) re-sent
  the last known setpoint, which the thermostat held as a temporary override until
  the next profile switch point. The frame now carries temperature byte 0, so the
  thermostat resumes its week profile. The target is no longer published
  optimistically in that case; the thermostat's own report updates it.

## 1.0.3

- Fixed: the MapleCUL stick dropped off the USB bus (and the config/time frame was
  lost) whenever two preamble sends (`Zs`) were queued back to back. This hit
  every config push (WakeUp followed 0.5 s later by the config frame) and the
  periodic time broadcast (1 s between rooms). Consecutive sends are now spaced
  by `TX_SETTLE` (2 s), so the stick stays connected and config frames are acked.
- The time broadcast now aborts when the CUL is unavailable instead of writing
  the remaining frames to a closed port.

## 1.0.2

- Fixed: switching a thermostat back to `auto` (e.g. after a manual/boost override)
  no longer forces the setpoint down to 17 °C until the next week-profile switch
  point. `SetTemperature` in auto mode always holds its temp byte until the
  thermostat's next scheduled switch point — the same way turning the physical
  dial does — so a bare `"auto"` command now re-sends the last known real setpoint
  instead of a hardcoded 17.0.

## 1.0.1

- Publish thermostat state (current/target temperature, valve, mode, battery,
  availability) as **retained** so Home Assistant restores the last known values
  after a restart instead of showing an empty entity until the next RF report.

## 1.0.0

- Initial release as a store add-on.
- Bridges ELV MAX! thermostats to MQTT via a (Maple)CUL/CUN stick.
- Reads weekly heating profiles from `/config/heating_week_profile.yaml`
  (`map: config:ro`).
- Thermostat room/address mapping is configurable via the `devices` option.
- Accepts the HVAC modes `auto`/`heat`/`off` directly on `set/desiredTemperature`.
- Boost (`heat`) now keeps the current setpoint instead of forcing a fixed 22 °C.
- Answers thermostat `TimeInformation` requests and broadcasts the time every
  `time_sync_interval_hours` (default 24) so the week-program clocks stay accurate.
- Per-room device configuration over MQTT (ConfigTemperatures): comfort/eco/min/max
  temperatures, `measurement_offset`, window-open temperature/duration — exposed as
  `climate/<room>/<setting>` state + `/set` command topics, persisted in `/data`.
- `boost_duration` and `boost_valve_position` configurable (ConfigValve). Config
  values cannot be read back over a CUL stick, so the add-on is the source of truth.
- Sends a `WakeUp` before each configuration frame so it is accepted on the first try.
