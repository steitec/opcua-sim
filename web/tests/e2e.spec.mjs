// The demo page in a real browser: Pyodide, the device fronts, the terminal queue,
// alarms, faults, load failure, no third-party requests, narrow screens.
import { test, expect } from "@playwright/test";

const id = (page, testId) => page.getByTestId(testId);

async function ready(page) {
  await page.goto("/");
  await expect(id(page, "status")).toContainText("ready", { timeout: 60_000 });
}

const terminal = (page) => id(page, "terminal-output");

async function type(page, line) {
  await id(page, "terminal-input").fill(line);
  await id(page, "terminal-input").press("Enter");
}

test("the tour runs from start to end", async ({ page }) => {
  await ready(page);
  await id(page, "chip-1").click();
  await expect(terminal(page)).toContainText("pump.DeviceHealth: 0 (NORMAL)");
  await expect(id(page, "session")).toHaveText("session open");

  await id(page, "chip-2").click();
  await expect(id(page, "pump-setpoint")).toHaveText("30.0 Hz");
  await expect(id(page, "pump-state")).toHaveText("running");
  await expect(id(page, "pump-speed")).not.toHaveText("0.0 Hz");

  await id(page, "chip-3").click();
  await expect(terminal(page)).toContainText("BadInvalidState");
  await expect(id(page, "pump-state")).toHaveText("tripped");
  await expect(id(page, "health-pump")).toHaveText("failure");
  await expect(id(page, "fault-badge-pump")).toHaveText("fault: trip");

  await id(page, "chip-4").click();
  await expect(id(page, "pump-state")).toHaveText("stopped");
  await expect(id(page, "health-pump")).toHaveText("normal");
  await expect(id(page, "fault-badge-pump")).toBeHidden();

  await id(page, "chip-5").click();
  await expect(terminal(page)).toContainText("pump.Overload severity=700 active=yes");
  await expect(terminal(page)).toContainText("acked=yes");
  await expect(id(page, "alarms")).toContainText("acknowledged");

  await id(page, "chip-6").click();
  await expect(id(page, "pressure-value")).toHaveText("no value");
  await expect(id(page, "pressure-status")).toHaveText("BadSensorFailure");
  await expect(id(page, "pressure-current")).toHaveText("3.60 mA");
  await expect(id(page, "health-tank_pressure")).toHaveText("failure");
  await expect(terminal(page)).toContainText("tank_pressure.Pressure: null  [BadSensorFailure");

  await id(page, "chip-7").click();
  await expect(terminal(page)).toContainText("ns=3;s=DB_Line.Speed: BadNodeIdUnknown");
  await expect(terminal(page)).toContainText('ns=3;s="DB_Line"."Speed": 0');
  await expect(terminal(page)).toContainText("[3] http://www.siemens.com/simatic-s7-opcua");
  await expect(id(page, "target-s7")).toHaveAttribute("aria-pressed", "true");

  await id(page, "chip-8").click();
  await expect(terminal(page)).toContainText("ns=4;i=6006: 2  [Good");
  await expect(id(page, "reader-status")).toHaveText("scanning");
});

test("an alarm is acknowledged with its button", async ({ page }) => {
  await ready(page);
  await type(page, "read pump.State");
  await type(page, "inject pump overload");
  const ack = id(page, "ack-pump.Overload");
  await expect(ack).toBeVisible();
  await ack.click();
  await expect(ack).toBeHidden();
  await expect(id(page, "alarms")).toContainText("acknowledged");
  await type(page, "clear pump overload");
  await expect(id(page, "alarms")).toHaveText("none");
});

test("the values live on", async ({ page }) => {
  await ready(page);
  const first = await id(page, "meter-energy").innerText();
  await expect(id(page, "meter-energy")).not.toHaveText(first, { timeout: 15_000 });
  await expect(id(page, "reader-ok")).not.toHaveText("0", { timeout: 5_000 });
  await expect(id(page, "reader-code")).not.toHaveText("–");
});

test("the commands keep their order", async ({ page }) => {
  await ready(page);
  await id(page, "fault-delay").click();
  await type(page, "read pump.State");
  await type(page, "write pump.SpeedSetpoint 12");
  await type(page, "read pump.SpeedSetpoint");
  await expect(terminal(page)).toContainText("pump.SpeedSetpoint: 12", { timeout: 15_000 });
  const lines = await terminal(page).locator(".line.command").allInnerTexts();
  expect(lines.slice(-3)).toEqual(["plant> read pump.State", "plant> write pump.SpeedSetpoint 12",
                                   "plant> read pump.SpeedSetpoint"]);
});

test("a silent link times out and a restart ends the session", async ({ page }) => {
  await ready(page);
  await type(page, "read pump.State");
  await id(page, "fault-blackhole").click();
  await type(page, "read pump.State");
  await expect(terminal(page)).toContainText("no answer in time", { timeout: 10_000 });
  await type(page, "clear server");
  await id(page, "fault-restart").click();
  await expect(id(page, "session")).toHaveText("server restarting");
  await type(page, "read pump.State");
  await expect(terminal(page)).toContainText("the server is restarting");
  await expect(id(page, "session")).toHaveText("no session", { timeout: 10_000 });
  await type(page, "read pump.State");
  await expect(id(page, "session")).toHaveText("session open");
});

test("what happens without a session is missed until refresh", async ({ page }) => {
  await ready(page);
  await type(page, "read pump.State");
  await type(page, "inject server drop_connections");
  await type(page, "inject pump overload");
  await type(page, "refresh");
  await expect(terminal(page)).toContainText("event(s) were missed");
  await expect(terminal(page)).toContainText("RefreshEndEvent");
  await expect(terminal(page)).toContainText("event  pump.Overload");
});

test("errors stay in the terminal", async ({ page }) => {
  await ready(page);
  await type(page, "nonsense");
  await expect(terminal(page)).toContainText("unknown command");
  await type(page, "help");
  await expect(terminal(page)).toContainText("inject TARGET");
  await type(page, "<img src=x onerror=alert(1)>");
  await expect(terminal(page)).toContainText("unknown command '<img");
  await expect(terminal(page).locator("img")).toHaveCount(0);
});

test("history with the arrow keys", async ({ page }) => {
  await ready(page);
  await type(page, "read pump.State");
  await type(page, "alarms");
  await id(page, "terminal-input").press("ArrowUp");
  await expect(id(page, "terminal-input")).toHaveValue("alarms");
  await id(page, "terminal-input").press("ArrowUp");
  await expect(id(page, "terminal-input")).toHaveValue("read pump.State");
});

test("reset brings everything back", async ({ page }) => {
  await ready(page);
  await id(page, "fault-trip").click();
  await expect(id(page, "pump-state")).toHaveText("tripped");
  await id(page, "reset").click();
  await expect(id(page, "pump-state")).toHaveText("stopped");
  await expect(id(page, "session")).toHaveText("no session");
  await expect(terminal(page)).toContainText("everything reset");
});

test("the wording says what runs here", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".explain")).toContainText("The OPC UA wire protocol is not");
});

test("no requests to other hosts, no errors", async ({ page, baseURL }) => {
  const foreign = [];
  const errors = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol.startsWith("http") && url.origin !== new URL(baseURL).origin) foreign.push(url.href);
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await ready(page);
  await id(page, "chip-1").click();
  await expect(terminal(page)).toContainText("pump.Speed");
  expect(foreign).toEqual([]);
  expect(errors).toEqual([]);
});

test("a failed load shows the fallback", async ({ page }) => {
  await page.route("**/py/manifest.json", (route) => route.fulfill({ status: 500, body: "no" }));
  await page.goto("/");
  await expect(id(page, "fallback")).toBeVisible({ timeout: 30_000 });
  await expect(id(page, "status")).toHaveText("could not start");
  const image = id(page, "fallback").locator("img");
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((node) => node.naturalWidth)).toBe(1200);   // a real picture
  await expect(id(page, "chip-1")).toBeDisabled();
});

test("works on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 700 });
  await ready(page);
  await id(page, "chip-2").click();
  await expect(id(page, "pump-state")).toHaveText("running");
  await id(page, "chip-7").click();
  await expect(terminal(page)).toContainText("BadNodeIdUnknown");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("privacy page", async ({ page }) => {
  await page.goto("/privacy.html");
  await expect(page.locator("h1")).toHaveText("Privacy");
});
