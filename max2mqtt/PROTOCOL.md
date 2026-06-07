# MAX! protocol coverage & missing features

This add-on currently implements only the small slice of the ELV/eQ-3 **MAX!**
RF protocol needed for everyday heating control (read state, set temperature/mode,
push week profiles). The thermostats support many more settings.

This document lists what is **implemented** vs. **missing**, using FHEM's
`10_MAX.pm` (device logic) and `14_CUL_MAX.pm` (CUL transport) as the protocol
reference, so the gaps can be implemented later without re-reverse-engineering.

Reference (FHEM mirror):
- `https://github.com/mhop/fhem-mirror/blob/master/fhem/FHEM/10_MAX.pm`
- `https://github.com/mhop/fhem-mirror/blob/master/fhem/FHEM/14_CUL_MAX.pm`

Conventions used below: temperatures are encoded as `int(temp * 2)` (0.5 °C
steps). A frame is `len cnt flags type src(3) dst(3) group payload`.

## Message-type coverage

| ID | Command | Dir | Status |
| --- | --- | --- | --- |
| `0x00` | PairPing | RX | ❌ not handled (no pairing) |
| `0x01` | PairPong | TX | ❌ not handled (no pairing) |
| `0x02` | Ack | RX | ✅ used to confirm sends |
| `0x03` | TimeInformation | RX/TX | ✅ answers requests + periodic broadcast |
| `0x10` | ConfigWeekProfile | TX | ✅ week profiles |
| `0x11` | ConfigTemperatures | TX | ✅ comfort/eco/min/max/offset/window |
| `0x12` | ConfigValve | TX | ✅ boost duration/valve (decalc/maxValve/valveOffset at defaults) |
| `0x20` | AddLinkPartner | TX | ❌ associate (e.g. window sensor) |
| `0x21` | RemoveLinkPartner | TX | ❌ deassociate |
| `0x22` | SetGroupId | TX | ❌ grouping |
| `0x23` | RemoveGroupId | TX | ❌ grouping |
| `0x30` | ShutterContactState | RX | ❌ window sensors unsupported |
| `0x40` | SetTemperature | TX | ⚠️ partial (auto/manual/boost; no "temporary/until") |
| `0x42` | WallThermostatControl | RX | ❌ wall thermostat unsupported |
| `0x43` | SetComfortTemperature | TX | ❌ comfort quick-set |
| `0x44` | SetEcoTemperature | TX | ❌ eco quick-set |
| `0x50` | PushButtonState | RX | ❌ eco/remote buttons |
| `0x60` | ThermostatState | RX | ⚠️ partial (no temporary "until" date) |
| `0x70` | WallThermostatState | RX | ❌ wall thermostat unsupported |
| `0x82` | SetDisplayActualTemperature | TX | ❌ wall thermostat display |
| `0xF0` | Reset | TX | ❌ factory reset |
| `0xF1` | WakeUp | TX | ✅ sent before config frames |

> **No config read-back over CUL.** The thermostats do not report their stored
> configuration (comfort/eco/offset/boost/decalc/…) over RF — only the original
> eQ-3 MAX! Cube (MAXLAN) can read it. The add-on is therefore the source of truth:
> it starts from factory defaults, is changed live over MQTT, and persists values in
> `/data`. Settings are written in bundled frames (ConfigTemperatures = 7 values,
> ConfigValve = 4 values), so changing one re-sends the whole group.

## Implemented today

- **SetTemperature (`0x40`)** with control modes auto (0), manual (1), boost (3).
  Payload: 1 byte `int(temp*2) | (ctrlmode << 6)`.
- **ConfigWeekProfile (`0x10`)** — weekly schedules per room.
- **ConfigTemperatures (`0x11`)** — comfort/eco/max/min/measurementOffset/windowOpen,
  per room over MQTT (see below).
- **ConfigValve (`0x12`)** — boost duration + valve position over MQTT;
  decalc/maxValve/valveOffset written at their persisted (factory) values.
- **WakeUp (`0xF1`)** — sent before each config frame so it lands on the first try.
- **TimeInformation (`0x03`)** — answers thermostat time requests and broadcasts the
  time every `time_sync_interval_hours` (default 24).
- **ThermostatState (`0x60`)** — parses mode, valve %, desired/actual temp, battery-low.
- **Ack (`0x02`)** — delivery confirmation, with duty-cycle aware resend.

### Config frame encodings (implemented)

- **ConfigTemperatures (`0x11`)** = 7 bytes `comfort, eco, max, min, offset, winTemp, winDur`;
  temps `int(t*2)`, `offset=int((measOffset+3.5)*2)`, `winDur=int(min/5)`.
- **ConfigValve (`0x12`)** = 4 bytes `boost, decalc, maxValve, valveOffset`;
  `boost=(durIdx<<5)|int(valve/5)` (durIdx 0–7 → 0,5,10,15,20,25,30,60 min),
  `decalc=(day<<5)|hour` (Sat=0…Fri=6), `maxValve/valveOffset=int(pct*255/100)`.
- **TimeInformation (`0x03`)** = 5 bytes `year-2000, day, hour, min|((mon&0x0C)<<4),
  sec|((mon&0x03)<<6)`; **WakeUp (`0xF1`)** payload `0x3F`.

## Missing settings & features

### 1. Temporary setpoint with end time — SetTemperature mode 2 ("until")
`0x40` supports a fourth control mode **temporary** (2): set a temperature that is
held until a date/time, then the device returns to its week program.
Payload: the 1 mode/temp byte **plus** 3 `until` bytes:
`((month & 0xE) << 20) | (day << 16) | ((month & 1) << 15) | ((year-2000) << 8) | (hour*2 + min/30)`
Useful for an HA "boost until 22:00" / vacation-style override.

### 2. Comfort / Eco quick-set (`0x43` / `0x44`)
Set the desired temperature directly to the stored comfort or eco value without
re-sending a number. (FHEM emulates this via SetTemperature; either approach works.)

### 3. Grouping & device linking
- **SetGroupId / RemoveGroupId (`0x22`/`0x23`)** — assign thermostats to a group
  so a single command (with the group flag `0x04`) addresses the whole room.
  Payload: 1 byte group id.
- **AddLinkPartner / RemoveLinkPartner (`0x20`/`0x21`)** — associate devices, most
  importantly a **ShutterContact (window sensor)** with a thermostat so the
  thermostat lowers to `windowOpenTemperature` while the window is open.
  Payload: `partnerAddr(3) + partnerType(1)`.

### 4. Wall-mounted thermostat support
- **WallThermostatState (`0x70`)** / **WallThermostatControl (`0x42`)** — receive
  state and the setpoint the wall unit dictates to its group.
- **SetDisplayActualTemperature (`0x82`)** — show measured (vs. target) temp on
  the wall display. Payload: 1 byte `0x04` (on) / `0x00` (off).

### 5. Other device classes
- **ShutterContactState (`0x30`)** — window open/closed sensors (could become HA
  `binary_sensor`s).
- **PushButtonState (`0x50`)** — MAX! eco switch / push buttons.

### 6. Pairing & factory reset
- **PairPing / PairPong (`0x00`/`0x01`)** — adopt a freshly reset device over RF
  instead of pre-knowing its address.
- **Reset (`0xF0`)** — factory-reset a device. No payload.

### 7. Richer ThermostatState parsing (`0x60`)
The current parser ignores the **temporary-mode "until" date** carried in the
state echo (bytes after the desired temperature). Parsing it would let HA show
when a temporary override ends.

## Suggested priority (for HA usefulness)

1. **Temporary "until" mode** — enables timed overrides from HA.
2. **ShutterContact (`0x30`) + linking (`0x20`)** — window-open automation.
3. **Wall thermostat (`0x70`/`0x42`/`0x82`)** — only if such devices are present.

> Duty cycle: every TX still costs the 868 MHz 1 % budget the bridge already
> accounts for, so batch config changes (and prefer WakeUp) when implementing.
