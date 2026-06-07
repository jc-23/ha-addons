# Changelog

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
