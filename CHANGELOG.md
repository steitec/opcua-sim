# Changelog

## 0.1.0 (2026-11-02)

First version.

- Five bundled profiles: pressure transmitter, temperature controller, drive, energy meter
  and a generic sensor; profiles and benches in YAML
- Device behaviour: ramps and a latching trip, a PI loop, 4-20 mA with NE 43 currents, a live
  energy counter
- `DeviceHealth` with the NE 107 states, analog items with `EURange` and `EngineeringUnits`,
  methods with arguments
- Alarms as OPC UA events: level and discrete alarms, `Acknowledge`, `ConditionRefresh`
- Runtime faults on devices (status codes, frozen values, old timestamps, drift, refused
  writes and calls, forced health, model faults) and on the server (delay, black hole, dropped
  and refused connections, server state, service level, restart)
- Security modes None, Sign and SignAndEncrypt (Basic256Sha256), user names with read-only users
- Command line, JSON control channel, pytest plugin
- Six profiles that present a simulated device the way a real product does, each naming its
  sources and limits: Siemens S7-1500, Beckhoff TwinCAT 3, Phoenix Contact PLCnext, CODESYS,
  Omron NJ/NX, Leuze BCL 300
