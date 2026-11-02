// The page: draws the device fronts from the simulator's state and runs the terminal.
// Every command goes through one queue, so output keeps the click order.

import { loadSim } from "./sim.js";

const TIMEOUT_MS = 2000;
const POLL_MS = 1000;
const CHIPS = [
  { label: "Read the pump and its health", steps: [["plant", "read pump.Speed pump.DeviceHealth"]] },
  { label: "Set 30 Hz and start it", steps: [["plant", "write pump.SpeedSetpoint 30"], ["plant", "call pump.Start"]] },
  { label: "Trip it: Start is now refused", steps: [["plant", "inject pump trip code=4"], ["plant", "call pump.Start"]] },
  { label: "Clear the cause, then Reset", steps: [["plant", "clear pump trip"], ["plant", "call pump.Reset"]] },
  { label: "Overload alarm, then acknowledge",
    steps: [["plant", "inject pump overload"], ["plant", "alarms"], ["plant", "ack pump.Overload on it"]] },
  { label: "Broken sensor: a Bad value is null",
    steps: [["plant", "inject tank_pressure sensor_break"], ["plant", "read tank_pressure.Pressure tank_pressure.OutputCurrent"]] },
  { label: "Siemens: the quotes belong to the ID",
    steps: [["s7", "read ns=3;s=DB_Line.Speed"], ["s7", 'read ns=3;s="DB_Line"."Speed"'], ["s7", "namespaces"]] },
  { label: "Leuze reader: numeric node IDs", steps: [["reader", "read ns=4;i=6006 ns=4;i=6205 ns=4;i=6043"]] },
];
const TARGET_LABEL = { plant: "plant", s7: "S7-1500", reader: "BCL 300" };
const HEALTH_CLASS = { NORMAL: "on", FAILURE: "alarm", CHECK_FUNCTION: "fault", OFF_SPEC: "fault", MAINTENANCE_REQUIRED: "fault" };
const HEALTH_TEXT = { NORMAL: "normal", FAILURE: "failure", CHECK_FUNCTION: "check function", OFF_SPEC: "off spec", MAINTENANCE_REQUIRED: "maintenance" };
const DRIVE_STATE = ["stopped", "running", "stopping", "tripped"];
const READER_STATE = ["idle", "error", "scanning", "busy"];

const $ = (selector) => document.querySelector(selector);
const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`);
const status = byTestId("status");
const output = byTestId("terminal-output");
const input = byTestId("terminal-input");
const controls = () => document.querySelectorAll(".console button, .console input");

let sim = null;
let target = "plant";
let latest = null;
let queue = Promise.resolve();
const history = [];
let historyAt = 0;

// --- drawing ------------------------------------------------------------------

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.textContent = text;
  return node;
}

// A Bad value has no number: null arrives here, and the front shows that instead of a stale one.
const fixed = (value, digits, unit) => (value == null ? "no value" : `${value.toFixed(digits)} ${unit}`);
const setText = (testId, text) => { byTestId(testId).textContent = text; };
const yesNo = (value) => (value ? "yes" : "no");

function showDevice(name, device) {
  const badge = byTestId(`fault-badge-${name}`);
  badge.hidden = device.faults.length === 0;
  badge.textContent = device.faults.map((kind) => `fault: ${kind.replaceAll("_", " ")}`).join(" · ");
  const health = byTestId(`health-${name}`);
  if (!health) return;
  health.textContent = HEALTH_TEXT[device.health];
  health.classList.remove("on", "alarm", "fault");
  health.classList.add(HEALTH_CLASS[device.health]);
}

function renderAlarms(endpoint) {
  const list = byTestId("alarms");
  const key = JSON.stringify(endpoint.alarms);
  if (list.dataset.key === key) return;   // keeps a button under the pointer alive between polls
  list.dataset.key = key;
  list.replaceChildren();
  if (endpoint.alarms.length === 0) list.append(el("li", { class: "none" }, "none"));
  for (const alarm of endpoint.alarms) {
    const item = el("li", { class: alarm.active ? "active" : "" });
    item.append(el("span", {}, `${alarm.id} · severity ${alarm.severity} · ${alarm.active ? "active" : "back to normal"}`));
    if (!alarm.acked) {
      const ack = el("button", { type: "button", "data-testid": `ack-${alarm.id}` }, "Acknowledge");
      ack.addEventListener("click", () => send(target, `ack ${alarm.id}`));
      item.append(ack);
    } else {
      item.append(el("span", { class: "acked" }, "acknowledged"));
    }
    list.append(item);
  }
}

function render(state) {
  latest = state;
  const d = state.devices;
  for (const name of Object.keys(d)) showDevice(name, d[name]);

  const pump = d.pump.values;
  setText("pump-speed", fixed(pump.Speed, 1, "Hz"));
  setText("pump-state", DRIVE_STATE[pump.State] ?? "–");
  setText("pump-setpoint", fixed(pump.SpeedSetpoint, 1, "Hz"));
  setText("pump-current", fixed(pump.MotorCurrent, 1, "A"));

  const tank = d.tank_pressure;
  setText("pressure-value", fixed(tank.values.Pressure, 2, "bar"));
  setText("pressure-current", fixed(tank.values.OutputCurrent, 2, "mA"));
  setText("pressure-status", tank.status.Pressure ?? "Good");

  const oven = d.oven.values;
  setText("oven-value", fixed(oven.ProcessValue, 1, "°C"));
  setText("oven-setpoint", fixed(oven.Setpoint, 1, "°C"));
  setText("oven-output", fixed(oven.Output, 1, "%"));
  $("#oven-bar").style.width = `${Math.max(0, Math.min(100, oven.Output ?? 0))}%`;

  const meter = d.meter.values;
  setText("meter-l1", fixed(meter.VoltageL1, 1, "V"));
  setText("meter-l2", fixed(meter.VoltageL2, 1, "V"));
  setText("meter-power", fixed(meter.ActivePower == null ? null : meter.ActivePower / 1000, 2, "kW"));
  setText("meter-energy", fixed(meter.Energy, 4, "kWh"));

  const plc = d.plc.values;
  setText("plc-run", yesNo(plc.Run));
  setText("plc-speed", fixed(plc.Speed, 0, "rpm"));
  setText("plc-parts", String(plc.Parts));
  setText("plc-fault", plc.Fault ? `yes, code ${plc.FaultCode}` : "no");

  const reader = d.reader.values;
  setText("reader-status", READER_STATE[reader.DeviceStatus] ?? "–");
  setText("reader-code", reader.LastScanData || "–");
  setText("reader-ok", String(reader.Ok));
  setText("reader-bad", String(reader.NotOk));

  const endpoint = state.endpoints[target];
  const session = byTestId("session");
  session.textContent = endpoint.restarting ? "server restarting" : endpoint.connected ? "session open" : "no session";
  session.classList.toggle("on", endpoint.connected);
  session.classList.toggle("alarm", endpoint.restarting);
  renderAlarms(endpoint);
  for (const event of state.events) print(`event  ${event.text}`, "event");
}

function print(text, kind = "") {
  output.append(el("div", { class: `line ${kind}` }, text));
  output.scrollTop = output.scrollHeight;
}

// --- running ------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function enqueue(job) {
  queue = queue.then(job).catch((error) => print(`internal error: ${error.message}`, "error"));
  return queue;
}

function setTarget(name) {
  target = name;
  for (const button of document.querySelectorAll("[data-target]")) {
    button.setAttribute("aria-pressed", String(button.dataset.target === name));
  }
  $("#prompt-label").textContent = `${TARGET_LABEL[name]}>`;
  if (latest) render({ ...latest, events: [] });
}

function send(to, line) {
  enqueue(async () => {
    print(`${TARGET_LABEL[to]}> ${line}`, "command");
    const result = sim.run(to, line);
    // the answer is ready at once; a slow server or a dead link is what the client would feel
    if (result.wait > 0) await sleep(result.wait * 1000);
    for (const out of result.lines) {
      if (out.kind === "timeout") await sleep(TIMEOUT_MS);
      print(out.text, out.kind);
    }
    render(result.state);
  });
}

function wire() {
  const chips = $("#chips");
  CHIPS.forEach((chip, i) => {
    const button = el("button", { type: "button", "data-testid": `chip-${i + 1}`, disabled: "" });
    button.append(el("span", { class: "step" }, String(i + 1)), el("span", {}, chip.label),
                  el("code", {}, chip.steps.map(([, line]) => line).join("  ·  ")));
    button.addEventListener("click", () => {
      for (const [to, line] of chip.steps) {
        setTarget(to);
        send(to, line);
      }
    });
    chips.append(button);
  });

  for (const button of document.querySelectorAll("[data-target]")) {
    button.addEventListener("click", () => setTarget(button.dataset.target));
  }
  $("#prompt").addEventListener("submit", (event) => {
    event.preventDefault();
    const line = input.value.trim();
    if (!line) return;
    history.push(line);
    historyAt = history.length;
    input.value = "";
    send(target, line);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    historyAt = Math.max(0, Math.min(history.length, historyAt + (event.key === "ArrowUp" ? -1 : 1)));
    input.value = history[historyAt] ?? "";
  });
  for (const button of document.querySelectorAll("[data-line]")) {
    button.addEventListener("click", () => {
      if (button.dataset.to) setTarget(button.dataset.to);
      send(target, button.dataset.line);
    });
  }
  byTestId("reset").addEventListener("click", () => {
    enqueue(async () => {
      render(sim.reset());
      print("# everything reset: devices back to power-on, faults cleared, sessions gone", "note");
    });
  });
  byTestId("retry").addEventListener("click", () => location.reload());
}

async function start() {
  wire();
  try {
    sim = await loadSim((text) => { status.textContent = text; });
  } catch (error) {
    document.body.classList.replace("loading", "failed");
    status.textContent = "could not start";
    $("#fallback-reason").textContent = `(${error.message})`;
    byTestId("fallback").hidden = false;
    return;
  }
  render(sim.state());
  // the devices live on: values change with time, so redraw them every second
  setInterval(() => { if (!document.hidden) render(sim.state()); }, POLL_MS);
  $("#version").textContent = sim.version;
  for (const control of controls()) control.disabled = false;
  document.body.classList.remove("loading");
  status.textContent = "ready – try the buttons below";
  print("# opcua-sim's device code is running in your browser. Type a command (help), or use the buttons.", "note");
}

start();
