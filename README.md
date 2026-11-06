<a href="https://steitec.net"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-dark.svg">
  <img align="right" height="32" alt="steitec" src="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-light.svg">
</picture></a>

# opcua-sim

[![CI](https://github.com/steitec/opcua-sim/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/steitec/opcua-sim/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/opcua-sim?v=0.1.0)](https://pypi.org/project/opcua-sim/)
[![Python](https://img.shields.io/pypi/pyversions/opcua-sim?v=0.1.0)](https://pypi.org/project/opcua-sim/)
[![License](https://img.shields.io/pypi/l/opcua-sim?v=0.1.0)](https://github.com/steitec/opcua-sim/blob/main/LICENSE)
[![Demo](https://img.shields.io/badge/demo-live%20in%20your%20browser-2ea44f)](https://opcua-sim.steitec.net)

Simulated OPC UA devices that **behave and fail**: a drive that ramps up after `Start` and
latches a trip until `Reset`, a transmitter whose broken sensor reads `BadSensorFailure` with
health `FAILURE` and 3.6 mA on the loop, alarms that arrive as real OPC UA events and wait for
your `Acknowledge`. Develop and test SCADA links, HMIs, gateways and data loggers without the
plant, also in CI. **[Try it in your browser](https://opcua-sim.steitec.net)**, no
install needed.

```console
$ opcua-sim serve --bench examples/plant.yaml &
opcua-sim: 4 device(s) on opc.tcp://127.0.0.1:4840, control on :4841
  oven  (temperature-controller)
  pump  (drive)
  tank_pressure  (pressure-transmitter)
  meter  (energy-meter)
$ opcua-sim write pump.SpeedSetpoint 30
pump.SpeedSetpoint: written 30.0
$ opcua-sim call pump.Start
pump.Start: ok
$ opcua-sim inject pump trip code=4
$ opcua-sim read pump.State pump.FaultCode pump.DeviceHealth
pump.State: 3  [Good, 2026-11-02T18:13:22+00:00]
pump.FaultCode: 4  [Good, 2026-11-02T18:13:22+00:00]
pump.DeviceHealth: 1  [Good, 2026-11-02T18:13:22+00:00]
$ opcua-sim call pump.Start
opcua-sim: The operation cannot be completed because the object is closed, uninitialized or in some other invalid state.(BadInvalidState)
$ opcua-sim inject tank_pressure sensor_break
$ opcua-sim read tank_pressure.Pressure tank_pressure.OutputCurrent
tank_pressure.Pressure: null  [BadSensorFailure, 2026-11-02T18:13:24+00:00]
tank_pressure.OutputCurrent: 3.6  [Good, 2026-11-02T18:13:24+00:00]
$ opcua-sim inject server delay seconds=5
$ opcua-sim read pump.Speed --timeout 1
opcua-sim: no answer in time
```

Walkthroughs, CI recipes and troubleshooting are in the
[wiki](https://github.com/steitec/opcua-sim/wiki); this README is the reference.

## Why

OPC UA demo servers are good at showing changing numbers. Client code breaks on other things:

- **quality**: a value that is `Bad` has no number at all, an `Uncertain` one has a number you
  may not trust, a frozen one has a source timestamp that stopped moving
- **device state**: methods refused in the wrong state, a latched trip, a health state that
  changes while the values still look fine
- **alarms**: conditions that must be acknowledged with the right `EventId` and have to be
  fetched again with `ConditionRefresh` after a reconnect
- **the connection**: late answers, a link that stays open and carries nothing, a server restart
  that takes every session and subscription with it

opcua-sim gives you all of that from a YAML profile, switchable at runtime. The OPC UA stack
underneath is [asyncua](https://github.com/FreeOpcUa/opcua-asyncio).

## What it is not

The generic profiles use the names of OPC UA DI (`DeviceSet`, `ParameterSet`, `MethodSet`,
`DeviceHealth`), but live in their own namespace and do not implement the DI companion
specification: a client that looks for the DI types will not find them.

The [product profiles](#product-profiles) copy how a real server presents its nodes, as far as
public sources describe it. None of them is a complete copy of a product, and none has been
compared with real hardware.

It is checked with asyncua and node-opcua clients. It has not been tested against UaExpert,
commercial SCADA systems or a conformance test tool. Please
[open an issue](https://github.com/steitec/opcua-sim/issues) if your client disagrees with it.

## Install

Requires Python 3.10 or newer.

```bash
pip install opcua-sim
```

From source, with the test dependencies:

```bash
git clone https://github.com/steitec/opcua-sim
cd opcua-sim
pip install -e ".[dev]"
pytest
```

## Profiles

| Profile | Device | Faults of its own |
|---|---|---|
| `pressure-transmitter` | 4-20 mA with NE 43 currents, `ZeroTrim`, `StartSelfTest`, level alarm | `sensor_break`, `over_range`, `maintenance_due` |
| `temperature-controller` | PI loop on a slow plant, setpoint, manual mode, `Start`, `Stop` | `sensor_break`, `heater_failure` |
| `drive` | frequency converter with ramps, `Start`, `Stop`, `Reset`, latching trip | `trip`, `overload`, `fan_worn` |
| `energy-meter` | three phases, power, an energy counter that counts, `ResetEnergy` | `phase_loss` |
| `generic-sensor` | signal sources, no behaviour: a starting point for your own | |

`opcua-sim map drive` shows the address space of a profile. Nodes are addressed by their string
ID in the namespace `urn:steitec:opcua-sim`, for example `ns=2;s=pump.Speed`:

```text
Objects/DeviceSet/pump
  Manufacturer, Model, SerialNumber, HardwareRevision, SoftwareRevision
  DeviceHealth        NORMAL, FAILURE, CHECK_FUNCTION, OFF_SPEC, MAINTENANCE_REQUIRED
  ParameterSet/       the variables; with a unit or range they are AnalogItemType
  MethodSet/          the methods
  Alarms/             one node per alarm: the ConditionId of its events
```

A bench puts several devices behind one server:

```yaml
name: demo plant
security: [none, encrypt]          # none, sign, encrypt; self-signed certificate
users:                             # optional; without users everybody may write
  anna: {password: s3cret}         # role operator
  vic: {password: look, role: viewer}
anonymous: true                    # anonymous clients may read
tick: 0.1                          # simulation step in seconds
seed: 0                            # noise is repeatable
devices:
  - {name: oven, profile: temperature-controller}
  - {name: pump, profile: drive, params: {ramp: 5.0}}
  - {name: mine, profile: my-device.yaml, serial_number: X-42}   # relative to this file
```

A profile of your own needs no Python. Without `model`, values come from signal sources
(`constant`, `sine`, `ramp`, `square`, `noise`), from `value`, or from what a client wrote:

```yaml
name: flow-sensor
identification: {manufacturer: ACME, model: F-100}
variables:
  - {name: Flow, unit: l/min, range: [0, 200], source: {kind: sine, offset: 100, amplitude: 20, period: 30}}
  - {name: Enabled, type: Boolean, value: true, writable: true}
  - {name: Limit, unit: l/min, value: 150, writable: true, min: 0, max: 200}
alarms:
  - {name: FlowLevel, kind: level, variable: Flow, high: 115, low: 85, deadband: 1,
     message: "Flow out of limits"}
```

Level alarms know `high_high`, `high`, `low` and `low_low`, by default with severity 900 for the
outer and 700 for the inner limits; `high: {value: 115, severity: 800}` changes that. Discrete
alarms (`kind: discrete`, `when: <flag>`) follow a flag of a device model. A value without a
valid status does not change the state of its level alarm.

## Product profiles

These profiles make the simulated server look like a real product to your client: same namespace
URIs and indices, same form of node IDs, same data types. A PLC server publishes whatever program
runs on it, so the variables are one small example program (run and reset bits, a ramp, a part
counter, a latching fault), presented the way each server would. That is what tests the parts
that break when you move between brands: quotes in Siemens IDs, the `|var|` of CODESYS, a namespace
index that differs per server.

| Profile | Example node ID | Based on | Assumed or missing |
|---|---|---|---|
| `siemens-s7-1500` | `ns=3;s="DB_Line"."Speed"` | Siemens application document 109737901 for the ID form, third-party guides for the URI and folders | folders and URI not confirmed by Siemens |
| `beckhoff-twincat3` | `ns=4;s=MAIN.fSpeed` | Beckhoff Information System for the URI, a Cybus guide for the IDs | real defaults are stricter: encrypted only, no anonymous access |
| `phoenix-plcnext` | `ns=2;s=Arp.Plc.Eclr/MainInstance.rSpeed` | PLCnext documentation for the URI and ID form | namespace index, folders |
| `omron-nx` | `ns=2;s=new_Controller_0.GlobalVars.Speed` | Omron manual W588 for the tree, types and status nodes | manual gives no URI or ID strings: **placeholders**, so browse |
| `codesys-control` | `ns=4;s=\|var\|CODESYS Control for Linux SL.Application.PLC_PRG.rSpeed` | forum reports only | URI is a **placeholder**, folders; the weakest of the six |
| `leuze-bcl300` | `ns=4;i=6205` | Leuze technical description 50141930 | 19 of the 136 listed nodes; no `ScanStart`, `Scan` |

`leuze-bcl300` is the one real device in the list: a bar code reader that scans by itself, with
node IDs, browse names and types from the manufacturer's table. Rockwell, Mitsubishi and gateways
such as KEPServerEX are not included, because the sources found were not good enough to copy them.

`opcua-sim map <profile>` prints every node ID with the sources and limits of that profile. These
profiles have no alarms and no `DeviceHealth` node, and a product profile must be the only device
of its bench, because it is a whole server. Their own faults are `trip` and `plc_stop` on the PLCs
and `read_fail` and `device_error` on the reader; every other fault works as usual.

```console
$ opcua-sim serve --profile siemens-s7-1500 --name plc &
$ opcua-sim write 'ns=3;s="DB_Line"."Run"' true
$ opcua-sim inject plc trip code=12
$ opcua-sim read 'ns=3;s="DB_Line"."Fault"' 'ns=3;s="DB_Line"."FaultCode"'
```

Methods need their object: `opcua-sim call 'ns=4;i=7002' --object 'ns=4;i=5001'`.

## Faults

```bash
opcua-sim inject <device> <kind> [name=value ...]
opcua-sim inject server <kind> [name=value ...]
opcua-sim clear <device or server> [kind]
```

**On every device:**

| Kind | Parameters | Effect |
|---|---|---|
| `bad_status` | `variable`, `status=BadCommunicationError` | the variable (or all) carries this status code; `Bad*` reads as null, `Uncertain*` keeps its value |
| `freeze` | `variable` | values stop changing, source timestamps stay old |
| `stale` | `seconds=60` | values go on, source timestamps lag behind |
| `drift` | `variable`, `per_second` | a numeric value runs away |
| `reject_write` | `variable`, `status=BadUserAccessDenied` | writes answer this status code |
| `method_fail` | `method`, `status=BadInvalidState` | calls answer this status code |
| `health` | `state` | forces `DeviceHealth` |

**On the server:**

| Kind | Parameters | Effect |
|---|---|---|
| `delay` | `seconds` | every response arrives late |
| `blackhole` | | connections stay open, nothing gets through |
| `drop_connections` | | all connections are cut now |
| `refuse` | | new connections are closed at once |
| `server_state` | `state` | `ServerStatus/State`: `Suspended`, `Test`, `Failed`, ... |
| `service_level` | `value` | `Server/ServiceLevel`, 0-255 |
| `restart` | `downtime=2` | the server goes away and returns on the same port without sessions or subscriptions; device state stays; building the server adds about a second |

Network faults sit in a small TCP forwarder in front of the server. `blackhole` holds the data
back rather than losing it, so a client that has not given up yet receives it after `clear`.

## Alarms

Alarms arrive as `ExclusiveLevelAlarmType` and `OffNormalAlarmType` events, from the device node
and from the `Server` object, with the usual fields: `ConditionId`, `ConditionName`, `Severity`,
`ActiveState/Id`, `AckedState/Id`, `Retain`, `Message`, and for level alarms the limits and
`LimitState/CurrentState`.

`Acknowledge` (`i=9111`, called on the `ConditionId`) checks the `EventId` and answers
`BadEventIdUnknown` or `BadConditionBranchAlreadyAcked`. `ConditionRefresh` (`i=3875`) sends
`RefreshStartEvent`, every retained condition, then `RefreshEndEvent`.

Alarm conditions are events, not full condition instances: `Enable`, `Disable`, `AddComment`,
`Confirm`, shelving and branches are not implemented, and the `Alarms/` nodes have no child
variables to browse.

## In pytest

Installing the package adds two fixtures. Every test gets a new bench on a free port:

```python
def test_notices_a_device_failure(opcua_bench):
    opcua_bench.inject("drive", "bad_status", variable="Speed", status="BadDeviceFailure")
    with pytest.raises(ua.uaerrors.BadDeviceFailure):
        read_speed(opcua_bench.url)       # your code


def test_two_servers(opcua_bench_factory):
    plant = opcua_bench_factory("plant.yaml")       # bench file, dict or profile name
    meter = opcua_bench_factory("energy-meter")
```

`--opcua-bench FILE` or the ini key `opcua_bench` choose the bench of `opcua_bench`; without them
it is one `drive`. See [`examples/test_example.py`](examples/test_example.py). The bench API is
`url`, `port`, `inject`, `clear`, `set`, `get`, `status`, `wait_restarted`.

Starting a server takes about a second, because asyncua loads the whole standard address space.

## Command line

| Command | |
|---|---|
| `serve --bench FILE` or `--profile NAME` | run; `--host`, `--port`, `--endpoint-host`, `--security`, `--certificate-dir` |
| `profiles`, `map PROFILE` | what is there |
| `browse`, `read`, `write`, `call`, `watch` | a small OPC UA client; `--url`, `--user`, `--password`, `--timeout` |
| `inject`, `clear`, `set`, `get`, `list` | the control channel of a running bench; `--control-port` |

`set` forces a value from outside (`null` hands it back to the simulation), `watch --alarms` prints
alarm events. The control channel is one JSON object per line on port 4841, described in
[`control.py`](src/opcua_sim/control.py).

`--endpoint-host` and `--certificate-dir` take their default from `OPCUA_SIM_ENDPOINT_HOST` and
`OPCUA_SIM_CERTIFICATE_DIR`. With a certificate directory, the server keeps its certificate
(`server-cert.der`) and key (`server-key.pem`) there: it uses the pair it finds, or creates one
on the first start, so a client has to trust the server only once. Put your own pair there to
use it instead.

## Docker

For clients that are not written in Python, such as an HMI, a SCADA system or a gateway: the
image runs the [demo plant](examples/plant.yaml) (`oven`, `pump`, `tank_pressure`, `meter`) with
an endpoint without security and one with Basic256Sha256 SignAndEncrypt.

```bash
docker run --rm --name sim -p 4840:4840 -p 127.0.0.1:4841:4841 ghcr.io/steitec/opcua-sim
```

The endpoint URL says `opc.tcp://localhost:4840`, and many clients connect to the URL the
server reports rather than the one you typed. So publish port 4840 as 4840, and where clients
reach the container under another name, tell the server:

```bash
docker run --rm -p 4840:4840 -e OPCUA_SIM_ENDPOINT_HOST=plc-sim ghcr.io/steitec/opcua-sim
```

The control channel has no authentication, hence `127.0.0.1:` in front of it. It listens on all
interfaces inside the container, so anything on the same Docker network can reach it. Inject a
fault without installing anything, or send the JSON lines to port 4841 from your own test code:

```bash
docker exec sim opcua-sim inject pump trip
docker exec sim opcua-sim clear pump
```

The certificate lives in `/certs` and is new for every container. Mount a directory there to
keep it, or to bring your own; it must be writable for UID 10001 unless both files are there:

```bash
mkdir certs && sudo chown 10001 certs
docker run --rm -p 4840:4840 -v "$PWD/certs":/certs ghcr.io/steitec/opcua-sim
```

Your own bench file, or other arguments, which replace the default command (keep
`--host 0.0.0.0`, or the port is only reachable from inside the container):

```bash
docker run --rm -p 4840:4840 -v "$PWD/bench.yaml":/bench/bench.yaml ghcr.io/steitec/opcua-sim
docker run --rm -p 4840:4840 ghcr.io/steitec/opcua-sim serve --profile siemens-s7-1500 --host 0.0.0.0
```

As a service container in GitHub Actions, for a job that runs on the runner itself (a job in a
container reaches the service as `opcua`; then set `OPCUA_SIM_ENDPOINT_HOST: opcua`):

```yaml
services:
  opcua:
    image: ghcr.io/steitec/opcua-sim
    ports:
      - 4840:4840
      - 127.0.0.1:4841:4841
```

## Security

Do not put this on a network you do not trust. It is a test tool:

- By default it binds to loopback. With `--host 0.0.0.0` the control channel, which has no
  authentication, is reachable too
- Passwords are plain text in the bench file, and on an endpoint without encryption they can
  cross the wire in plain text
- Certificates are self-signed and created at every start unless there is a certificate
  directory; client certificates are not checked

## Browser demo

**[Try it at opcua-sim.steitec.net](https://opcua-sim.steitec.net).** The page runs the real device
code in the browser with [Pyodide](https://pyodide.org), but not the OPC UA wire protocol, because a
browser has no TCP sockets. To test a client, install opcua-sim. The source is in [`web/`](web/).

## License

[MIT](LICENSE). The OPC UA stack [asyncua](https://github.com/FreeOpcUa/opcua-asyncio) is LGPL-3.0
and is installed as a separate package.
