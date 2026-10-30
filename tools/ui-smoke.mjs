import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { chromium, webkit } from "playwright";

const root = process.cwd();
const artifacts = process.env.ARTIFACTS || (await import("node:os")).tmpdir();
const prefix = "/api/hassio_ingress/dashboard-test";
const defaults = JSON.parse(execFileSync("python3", ["-c", "import json,sys; sys.path.insert(0,'nibe_comfort'); from controller import DEFAULTS; print(json.dumps(DEFAULTS))"], { encoding: "utf8" }));
const offset = "number.pump_heating_offset_climate_system_1";
const outdoor = "sensor.pump_current_outd_temp_bt1";
const price = "sensor.electricity_price_home";
const thermostat = "climate.lk_allrum_thermostat";
let settings = { ...defaults, enabled: true, room_sensors: ["sensor.living"], offset_entity: offset, outdoor_entity: outdoor, base_offset: 3 };
let writes = 0;
let pumpTest = null;
let testWrites = 0;
let failHistory = false;
const now = Date.now() / 1000;
const entities = [
  { entity_id: "sensor.living", name: "Vardagsrum", state: "22", unit: "°C", device_class: "temperature" },
  { entity_id: thermostat, name: "LK Allrum", state: "heat", current_temperature: 22.2, temperature_error: null, setpoint: 26, temperature_unit: "°C" },
  { entity_id: offset, name: "NIBE Offset", state: "3", min: -10, max: 10, step: 1 },
  { entity_id: outdoor, name: "NIBE BT1", state: "11", unit: "°C", device_class: "temperature" },
  { entity_id: price, name: "Tibber elpris", state: "1.4", unit: "SEK/kWh" },
  { entity_id: "climate.lk_badrum_overvaningen_thermostat_with_a_very_long_identifier", name: "LK Badrum övervåningen Thermostat med extra långt namn", state: "heat", current_temperature: 25.7, temperature_error: null },
  { entity_id: "sensor.nibe_f730_heating_offset_smart_price_adaption_extra_long", name: "NIBE F730 Heating offset smart price adaption", state: "0", unit: "°C", device_class: "temperature" },
  { entity_id: "weather.smhi_home", name: "SMHI hemma", state: "cloudy" },
  { entity_id: "sensor.pump_energy_consumed", name: "Pumpens använda el", state: "1234", unit: "kWh",
    device_class: "energy", state_class: "total_increasing", energy_meter_error: null },
  { entity_id: "sensor.pump_el_current_power_consumption", name: "Pumpens totala eleffekt", state: "2", unit: "kW",
    device_class: "power", state_class: "measurement", energy_meter_error: null },
  { entity_id: "sensor.pump_10_years_consumption", name: "10 years consumption", state: "15000", unit: "kWh",
    device_class: "energy", state_class: "total_increasing", energy_meter_error: "Periodräknare stöds inte här." },
  { entity_id: "sensor.pump_energy_produced_for_heating_current_hour", name: "Producerad värme denna timme",
    state: "2", unit: "kWh", device_class: "energy", energy_meter_error: "Producerad energi är inte använd el." },
];
const areaMap = {
  "sensor.living": { area_id: "living", area_name: "Vardagsrum" },
  [thermostat]: { area_id: "allrum", area_name: "Allrum" },
  "climate.living": { area_id: "living", area_name: "Vardagsrum" },
  "climate.lk_badrum_overvaningen_thermostat_with_a_very_long_identifier": { area_id: "bathroom", area_name: "Badrum" },
};
const history = () => ({
  start: now - 86400, end: now, target: settings.target_temperature,
  series: settings.room_sensors.map((id, i) => ({
    entity_id: id, name: entities.find((e) => e.entity_id === id).name, outdoor: false,
    kind: id.startsWith("climate.") ? "climate" : "sensor",
    ...(id.startsWith("climate.") ? { setpoints: [[now - 86400, 22], [now - 7200, 23]], points: [[now - 86400, 21.6], [now - 30000, 21.9], [now - 3000, 22.4]] }
      : { points: Array.from({ length: 25 }, (_, index) => [now - (24 - index) * 3600, index === 10 ? null : 21.5 + Math.sin(index / 3) + i * 0.2]) }),
  })),
});
const events = [
  { id: 5, timestamp: now, kind: "write_observed", message: "Begärt värde återläst från Home Assistant.", details: { request_id: 3, requested: 2, observed: 2 } },
  { id: 4, timestamp: now - 5, kind: "write_sent", message: "Home Assistant accepterade serviceanropet; inväntar återläsning.", details: { request_id: 3 } },
  { id: 3, timestamp: now - 10, kind: "write_attempt", message: "Automatiken begär ändrad kurvförskjutning.", details: { previous: 3, value: 2, indoor: 22, target_temperature: 21, contributions: { room: -1.5, weather: 0, price: 0 } } },
  { id: 2, timestamp: now - 100, kind: "settings_saved", message: "Inställningar sparade.", details: { changes: { enabled: { from: false, to: true } } } },
];
const overview = {
  checked_at: now,
  pump: [
    { key: "compressor", label: "Kompressor", value: "I drift", reported_at: new Date().toISOString() },
    { key: "alarm", label: "Pumplarm", value: "Larm aktivt", active: true, reported_at: new Date().toISOString() },
    { key: "frequency", label: "Kompressorfrekvens", value: "45", unit: "Hz" },
    { key: "additional_heat", label: "Eltillsats", value: null, error: "Mätvärdet är för gammalt." },
  ],
  pump_electricity: [],
  house: [
    { label: "Husets effekt just nu", value: "1200", unit: "W" },
    { label: "Husets förbrukning i dag", value: "12", unit: "kWh" },
  ],
  price: { label: "Aktuellt elpris", value: "1.4", unit: "SEK/kWh" },
  electricity_note: "Husets Tibber-värden gäller hela huset, inte bara pumpen.",
};
const server = createServer(async (request, response) => {
  const path = request.url.slice(prefix.length);
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  let data;
  if (path === "/api/settings" && request.method === "PUT") {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    settings = JSON.parse(Buffer.concat(chunks)); writes++;
    data = settings;
  } else if (path === "/api/settings") data = settings;
  else if (path === "/api/entities") data = entities;
  else if (path === "/api/discovery") data = {
    offset_entity: offset, outdoor_entity: outdoor, price_entity: price,
    message: "NIBE hittad", price_message: "Tibbers aktuella elpris hittades automatiskt.",
  };
  else if (path === "/api/status") data = {
    indoor: 22, outdoor: 10, current_offset: 3, desired_offset: 1.5, next_offset: 2,
    enabled: settings.enabled, checked_at: now, wait_seconds: 1800,
    message: "Väntar på nästa tillåtna ändring.", contributions: { room: -1.5, weather: 0, price: 0 },
    house_indoor: 22, warm_rooms: [], rooms: settings.room_sensors.map((id) => ({
      entity_id: id, name: entities.find((entity) => entity.entity_id === id)?.name || id,
      temperature: 22, priority: settings.priority_sensors.includes(id),
    })),
  };
  else if (path === "/api/version") data = { version: "0.2.8" };
  else if (path === "/api/test" && request.method === "PUT") {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const { action } = JSON.parse(Buffer.concat(chunks)); testWrites++;
    if (action === "start") pumpTest = { phase: "running", entity_id: offset, original: 2, value: 3, started_at: now, restore_at: now + 600, observed_at: now + 60, restored_at: null, finished_at: null, result: null };
    else pumpTest = { ...pumpTest, phase: "done", restored_at: now + 120, finished_at: now + 180, result: "confirmed" };
    data = { now, duration_seconds: 600, test: pumpTest };
  }
  else if (path === "/api/test") data = { now, duration_seconds: 600, test: pumpTest };
  else if (path === "/api/overview") data = overview;
  else if (path === "/api/plan") data = {
    start: now - 86400, end: now + 36 * 3600, now,
    outdoor: { history: Array.from({ length: 24 }, (_, i) => [now - (24 - i) * 3600, 5 + Math.sin(i / 4) * 3]), forecast: Array.from({ length: 36 }, (_, i) => [now + i * 3600, 4 - i / 6]), error: null },
    prices: { points: Array.from({ length: 240 }, (_, i) => [now - 86400 + i * 900, 1 + Math.sin(i / 20) * 0.6]), low: 0.6, high: 1.5, unit: "SEK/kWh", error: null },
    offset: { history: [[now - 86400, 3], [now - 20000, 2]], planned: [[now, 2], [now + 30000, 3]], desired: [[now, 2.3], [now + 30000, 3.1]], changes: [{ timestamp: now + 30000, offset: 3, previous: 2, desired: 3.1, reasons: ["billigt elpris (0.5)"] }], done: [{ timestamp: now - 20000, previous: 3, offset: 2, source: "automatik", reasons: ["rum -1"] }, { timestamp: now - 80000, previous: 2, offset: 3, source: "manuell eller annan automation", reasons: [] }], min: -3, max: 3, base: 3, error: null },
    curve: { points: Array.from({ length: 60 }, (_, i) => [8 - i / 6, 28 + i / 5 + (i > 40 ? 0 : 1), i > 40 ? 1 : 2]), fit: { slope: -1.2, intercept: 38, min_outdoor: -2, max_outdoor: 8, count: 40, offset: 2 }, current: { outdoor: 1.5, supply: 33 }, forecast_range: [-2, 4], supply_entity: "sensor.x", error: null },
    notes: ["Väder- och elprispåverkan är 0, så planen följer bara rumstemperaturen."],
  };
  else if (path === "/api/hotwater") {
    const draws = [6, 5.5, 4.5, 3.5, 2.5, 1.5, 0.5].flatMap((day) => [
      { start: now - day * 86400 - 3600, end: now - day * 86400 - 2700, from: 50, to: 33, drop: 17, size: "stor" },
      { start: now - day * 86400 - 20000, end: now - day * 86400 - 19800, from: 50, to: 48, drop: 2, size: "liten" },
    ]).filter((event) => event.start > now - 7 * 86400).sort((a, b) => a.start - b.start);
    const charge = [[now - 7 * 86400, 50]];
    for (const event of draws) charge.push([event.start, event.from], [event.end, event.to], [event.end + 2400, 50]);
    data = {
      now, start: now - 7 * 86400, days: 7, entities: {}, top: 50.4, charge: 44, minutes: 14, demand: "Normal", charging: false,
      level: 0.5, level_source: "minutes", max_minutes: 28,
      series: { top: [[now - 7 * 86400, 51], [now - 3600, 50.4]], charge, minutes: [[now - 7 * 86400, 28], [now - 3600, 14]] },
      draw_offs: draws, charges: draws.map((event) => ({ start: event.end + 60, end: event.end + 2400 })), error: null,
    };
  }
  else if (path === "/api/learning") {
    const hour = 3600;
    data = {
      now, error: null, target: settings.target_temperature,
      model: { status: "rough", hours: 159, days: 6.6, changes: 4, needed_hours: 168, needed_changes: 6, lag: 0, skill: 0.21, r2: 0.06,
        facts: { time_constant_hours: 10.2, degrees_per_step: 0.19, outdoor_effect: 0, lag_hours: 0, step_after_3h: 0.05, step_after_6h: 0.08, half_response_hours: 7.1 },
        message: "Grov uppskattning (6,6 av 7 dygn, 4 av 6 kurvändringar). Förvärmningen använder försiktiga standardvärden (3 timmar innan) tills modellen är säker." },
      history: Array.from({ length: 24 }, (_, i) => [now - (24 - i) * hour, 21.7 + Math.sin(i / 4) * 0.3]),
      prediction: Array.from({ length: 25 }, (_, i) => [now + i * hour, 21.9 + (i > 6 && i < 10 ? 0.3 : 0) - (i >= 10 && i < 13 ? 0.2 : 0)]),
      fireplace_periods: [{ start: now - 7200, end: now - 3600 }],
      preheat: { enabled: settings.preheat_enabled, degrees: settings.preheat_degrees, lead_hours: 3, lead_source: "default", note: null,
        windows: [{ kind: "preheat", start: now + 7 * hour, end: now + 10 * hour, shift: 0.5, price: 0.9 },
                  { kind: "coast", start: now + 10 * hour, end: now + 13 * hour, shift: -0.5, price: 2.4 }] },
    };
  }
  else if (path === "/api/events") data = events;
  else if (path === "/api/history") {
    if (failHistory) { response.writeHead(502); response.end('{"error":"Recorder saknas"}'); return; }
    data = history();
  } else if (path === "/api/areas") data = areaMap;
  else if (path === "/api/forecast") data = {
    name: "SMHI hemma", entity_id: "weather.smhi_home", type: "hourly", fetched_at: now,
    note: "Prognosen ändrar inte värmestyrningen.",
    points: Array.from({ length: 25 }, (_, i) => ({ timestamp: now + i * 3600, temperature: 10 + Math.cos(i / 3) * 3 })),
  };
  else if (request.url === "/ha-parent") {
    // Same-origin stand-in for the Home Assistant frontend around the ingress iframe.
    response.setHeader("Content-Type", "text/html");
    response.end(`<!doctype html><html style="--primary-background-color:#111111"><body style="margin:0;background:#111111"><iframe id="panel" src="${prefix}/" style="width:390px;height:800px;border:0"></iframe></body></html>`);
    return;
  } else {
    const file = path === "/" ? "index.html" : path.slice(1);
    if (!["index.html", "app.js", "theme.js", "style.css"].includes(file)) { response.writeHead(404); response.end("{}"); return; }
    response.setHeader("Content-Type", { "index.html": "text/html", "app.js": "text/javascript", "theme.js": "text/javascript", "style.css": "text/css" }[file]);
    response.setHeader("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'self'");
    response.end(await readFile(`${root}/nibe_comfort/static/${file}`));
    return;
  }
  response.end(JSON.stringify(data));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch();
const errors = [];
async function dashboardChecks() {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/entities", (route) => route.fulfill({
    status: 502, json: { error: "Tillfälligt anslutningsfel" },
  }), { times: 1 });
  await page.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await page.waitForFunction(() => document.getElementById("notice").textContent.includes("Försöker igen automatiskt"));
  await page.waitForFunction(() => document.getElementById("forecast-chart").querySelector("svg"));
  assert.equal(await page.locator("#notice").isVisible(), false, "Initialization recovers without reloading");
  await page.waitForFunction(() => document.querySelectorAll("#events-list article").length === 4);
  await page.waitForFunction(() => document.getElementById("plan-curve").querySelector("svg"));
  const initialWrites = writes;
  for (const id of ["plan-details", "curve-details", "learning-details", "hot-water-details", "history-decisions", "electricity-details", "comfort-details"]) {
    assert.equal(await page.locator(`#${id}`).getAttribute("open"), null, `${id} starts folded`);
  }
  assert.equal(await page.locator("#plan-offset").isVisible(), false);
  assert.equal(await page.locator("#hw-verdict").isVisible(), true);
  assert.equal(await page.locator("#comfort-status").isVisible(), true);
  assert.equal(await page.locator("#pump-metrics").isVisible(), true, "Pump alarms are never hidden by the new disclosures");
  await page.screenshot({ path: `${artifacts}/simplified-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${artifacts}/simplified-mobile.png` });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: `${artifacts}/simplified-dark.png` });
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.locator('.page-nav a[href="#settings-panel"]').click();
  assert.equal(await page.locator("#settings-panel").getAttribute("open"), "");
  await page.locator("#settings-panel > summary").click();
  await page.locator('.page-nav a[href="#log-panel"]').click();
  assert.equal(await page.locator("#log-panel").getAttribute("open"), "");
  await page.locator("#log-panel > summary").click();
  await page.locator("#plan-details > summary").focus();
  await page.keyboard.press("Enter");
  assert.equal(await page.locator("#plan-offset").isVisible(), true, "Details work using the keyboard");
  await page.waitForFunction(() => {
    const chart = document.getElementById("plan-offset");
    return Number(chart.querySelector("svg").getAttribute("viewBox").split(" ")[2]) === Math.round(chart.clientWidth);
  });
  for (const id of ["curve-details", "learning-details", "hot-water-details"]) {
    await page.locator(`#${id} > summary`).click();
  }
  assert.equal(writes, initialWrites, "Navigation and disclosures must never save or control anything");
  assert.equal(await page.locator("#plan-outdoor path").count(), 2);
  assert.equal(await page.locator("#plan-price path").count(), 1);
  assert.equal(await page.locator("#plan-offset path").count(), 3);
  assert.equal(await page.locator("#plan-offset circle").count(), 3);
  assert.equal(await page.locator("#plan-offset .chart-event.done-other").count(), 1);
  assert.equal(await page.locator("#plan-offset .chart-event.planned").count(), 1);
  assert.equal(await page.locator("#plan-offset .chart-event.done-auto").count(), 1);
  assert.match(await page.locator("#plan-offset .chart-event.planned text").textContent(), /▲ 2→3/);
  const changes = await page.locator("#plan-changes").textContent();
  assert.match(changes, /Kommande justeringar.*▲ 2→3 · billigt elpris/);
  assert.match(changes, /Gjorda ändringar.*▼ 3→2 · automatik \(rum -1\)/);
  assert.match(changes, /▲ 2→3 · manuell eller annan automation/);
  assert.match(await page.locator("#plan-curve-status").textContent(), /0 °C ute/);
  // Simple summary first: inside, wanted, outside and next adjustment.
  assert.notEqual(await page.locator("#indoor").textContent(), "—");
  assert.match(await page.locator("#indoor-diff").textContent(), /(under|över) det du vill ha|Precis/);
  assert.equal(await page.locator("#target-label").textContent(), "Mål för valda rum · °C");
  assert.match(await page.locator("#target-scope").textContent(), /snittet av alla 1 valda rum/);
  assert.match(await page.locator("#outdoor-now").textContent(), /^\d/);
  assert.match(await page.locator("#outdoor-forecast").textContent(), /Närmaste dygnet .* °C · kallast/);
  assert.match(await page.locator("#next-change").textContent(), /^(kl|i morgon) \d\d:\d\d$/);
  assert.match(await page.locator("#next-change-why").textContent(), /Höjer värmen ett steg \(2→3\) · billigt elpris/);
  assert.equal(await page.locator("#hero-offset").textContent(), "3");
  assert.equal(await page.locator("#hero-offset").isVisible(), true);
  assert.match(await page.locator("#hero-offset-base").textContent(), /grundvärde 3/);
  assert.match(await page.locator("#hero-extra").textContent(), /Elpris nu .* SEK\/kWh \((billigt|normalt|dyrt)\)/);
  await page.waitForFunction(() => document.getElementById("hw-status").textContent.startsWith("Uppdaterad"));
  await page.waitForFunction(() => document.getElementById("learn-status").textContent.startsWith("Grov bild"));
  await page.waitForFunction(() => document.getElementById("version").textContent === " · Version 0.2.8");
  assert.equal(await page.locator("#learning-card").evaluate((card) => card.previousElementSibling.querySelector("#plan-changes") !== null), true, "Learning card follows the plan");
  assert.equal(await page.locator("#hot-water-card").evaluate((card) => card.previousElementSibling.id), "fireplace-card");
  assert.match(await page.locator("#learn-progress").textContent(), /6,6 av 7 dygn.*4 av 6/);
  assert.match(await page.locator("#learn-facts").textContent(), /\+0,19 °C inne på sikt.*≈ 7,1 timmar/);
  assert.equal(await page.locator("#learn-chart path").count(), 2);
  assert.equal(await page.locator("#learn-chart .chart-band.preheat").count(), 1);
  assert.equal(await page.locator("#learn-chart .chart-band.coast").count(), 1);
  assert.equal(await page.locator("#learn-chart .chart-band.fireplace").count(), 1);
  assert.match(await page.locator("#learn-windows").textContent(), /Förvärmning är av.*3 timmar före \(standard.*förvärm \+0,5°.*0,5° svalare/);
  assert.equal(await page.locator("#preheat_enabled").isChecked(), false, "Pre-heating starts off");
  assert.equal(await page.locator("#preheat_degrees").inputValue(), "0.5");
  assert.match(await page.locator("#preheat-example").textContent(), /vanligt mål 21 °C.*före dyrperiod 21,5 °C.*under dyrperiod 20,5 °C.*tillbaka till 21 °C.*Planeringen är av/);
  await page.locator("#preheat_degrees").fill("0.3");
  await page.locator("#preheat_enabled").check();
  assert.match(await page.locator("#preheat-example").textContent(), /osparade.*före dyrperiod 21,3 °C.*under dyrperiod 20,7 °C.*först när du sparar/);
  assert.match(await page.locator("#settings-save-status").textContent(), /Osparade.*Förvärmning/);
  assert.equal(writes, 0, "Preheat preview must not change saved settings");
  await page.locator("#preheat_degrees").fill("0.5");
  await page.locator("#preheat_enabled").uncheck();
  assert.equal(await page.locator("#hw-verdict").textContent(), "Varmvattnet räcker bra");
  assert.match(await page.locator("#hw-tank").textContent(), /50%/);
  assert.match(await page.locator("#hw-verdict-text").textContent(), /ungefär 14 minuter.*Senast laddat/);
  assert.match(await page.locator("#hw-pattern").textContent(), /Senaste 7 dygnen: \d+ tappningar .* stora som dusch eller bad\. Stora tappningar sker oftast kl \d\d–\d\d/);
  assert.equal(await page.locator("#hw-hours rect.hw-bar").count() > 0, true);
  assert.ok(await page.locator("#hw-chart .chart-event.hw-draw").count() >= 2);
  assert.ok(await page.locator("#hw-chart .chart-band.hw-charge").count() >= 2);
  assert.equal(await page.locator("#hw-list li").count(), 8);
  assert.match(await page.locator("#hero-extra").textContent(), /Varmvatten 50% \(14 min\)/);
  assert.equal(await page.locator("#settings-panel").getAttribute("open"), null, "Configured settings start folded");
  assert.equal(await page.locator("#log-panel").getAttribute("open"), null);
  await page.locator("#settings-panel > summary").click();
  await page.locator("#priority_sensors input").check();
  assert.match(await page.locator("#preheat-example").textContent(), /osparade.*prioriterade rum/);
  assert.equal(await page.locator("#target-label").textContent(), "Mål för prioriterade rum · °C");
  assert.match(await page.locator("#target-scope").textContent(), /snittet av 1 prioriterat rum/);
  await page.locator("#priority_sensors input").uncheck();
  assert.equal(await page.locator("#target-label").textContent(), "Mål för valda rum · °C");
  await page.locator("#settings-panel > summary").click();
  assert.equal(await page.locator("#save-target").isVisible(), false);
  await page.locator("#target-up").click();
  assert.equal(await page.locator("#save-target").isVisible(), true);
  await page.locator("#target-down").click();
  assert.equal(await page.locator("#save-target").isVisible(), false);
  assert.equal(await page.locator("#enabled").isChecked(), true);
  assert.equal(await page.locator("#price_entity").inputValue(), price);
  assert.equal(await page.locator("#manual-price").getAttribute("open"), null);
  assert.equal(await page.locator("#fixed-price-settings").getAttribute("open"), null);
  assert.match(await page.locator("#fixed-price-settings").textContent(), /separat regel utan framförhållning/);
  assert.match(await page.locator("#price-unit").textContent(), /bekräftar inte att kommande priser finns/);
  assert.equal(writes, 0, "Discovery and dashboard reads must not save or activate anything");
  assert.equal(await page.locator("#temperature-chart path").count(), 1);
  assert.equal(await page.locator("#forecast-chart path").count(), 1);
  assert.match(await page.locator("#pump-metrics").textContent(), /Larm aktivt/);
  assert.equal(await page.locator("#pump-metrics .alarm-active").count(), 1);
  assert.match(await page.locator("#electricity-metrics").textContent(), /Pumpens elförbrukning.*Okänt/);
  assert.match(await page.locator("#electricity-note").textContent(), /hela huset/);
  assert.match(await page.locator("#events-list").textContent(), /Förskjutning 3 → 2/);
  assert.match(await page.locator("#events-list").textContent(), /Automatik: av → på/);
  assert.equal(await page.locator("#test-panel").getAttribute("open"), null);
  assert.equal(await page.locator("#test-panel").evaluate((panel) => panel.nextElementSibling.id), "log-panel");
  await page.locator("#test-panel > summary").click();
  assert.match(await page.locator("#test-status").textContent(), /Inget test/);
  assert.equal(await page.locator("#test-stop").isVisible(), false);
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#test-start").click();
  await page.waitForFunction(() => document.getElementById("test-status").textContent.includes("Testet pågår"));
  assert.match(await page.locator("#test-steps").textContent(), /2 → 3.*rapporterar 3.*förskjutningen visar 3.*automatiskt till 2/);
  assert.equal(await page.locator("#test-steps li.done").count(), 2);
  assert.match(await page.locator("#test-look").textContent(), /Titta på pumpen nu: förskjutningen ska visa 3/);
  assert.match(await page.locator("#test-base-note").textContent(), /grundvärde är 3.*justerat förskjutningen till 2/);
  assert.deepEqual(await page.locator(".test-values strong").allTextContents(), ["2", "3", "3"]);
  assert.equal(await page.locator(".test-where").getAttribute("open"), null, "Pump menu details start folded");
  assert.equal(await page.locator("#test-start").isVisible(), false);
  await page.locator("#test-card").screenshot({ path: `${artifacts}/test-running.png` });
  await page.locator("#test-stop").click();
  await page.waitForFunction(() => document.getElementById("test-status").textContent.includes("Styrningen fungerar"));
  assert.equal(await page.locator("#test-steps li.done").count(), 4);
  assert.match(await page.locator("#test-look").textContent(), /ska nu visa 2 igen/);
  assert.equal(testWrites, 2);
  assert.equal(writes, 0, "The pump test must not save settings");
  await page.locator("#settings-panel > summary").click();
  await page.locator("#log-panel > summary").click();
  const climateInput = page.locator(`#room_sensors input[value="${thermostat}"]`);
  assert.equal(await climateInput.count(), 1);
  assert.match(await climateInput.locator("..").textContent(), /22,2 °C/);
  await climateInput.check();
  await page.locator("#save").click();
  await page.waitForFunction(() => document.getElementById("notice").textContent.includes("sparade"));
  await page.waitForFunction(() => document.querySelectorAll("#temperature-chart path").length === 2);
  assert.equal(settings.enabled, true);
  assert.equal(settings.price_entity, price);
  assert.equal(settings.weather_gain, 0);
  assert.equal(settings.expensive_reduction, 0);
  assert.equal(settings.preheat_enabled, false);
  assert.ok(settings.room_sensors.includes(thermostat));
  assert.equal(writes, 1);
  await page.locator("#history-entity").selectOption(thermostat);
  assert.equal(await page.locator("#temperature-chart path.dashed").count(), 1);
  assert.match(await page.locator("#history-status").textContent(), /inställd att värma till 23 °C/);
  assert.match(await page.locator("#temperature-legend").textContent(), /uppmätt.*inställd/);
  for (const key of Object.keys(settings)) assert.equal(await page.locator(`#${key}`).count(), 1, key);
  assert.equal(await page.locator("svg path").evaluateAll((paths) => paths.every((path) => !/NaN|Infinity/.test(path.getAttribute("d")))), true);
  await page.screenshot({ path: `${artifacts}/dashboard-desktop.png`, fullPage: true });
  await page.locator("#plan-offset").screenshot({ path: `${artifacts}/offset-desktop.png` });
  const shots = process.env.SHOTS;
  if (shots) {
    await page.setViewportSize({ width: 1280, height: 1100 });
    await page.waitForTimeout(1200);
    const cards = page.locator("main > section.card:not(#comfort-card)");
    await page.evaluate(() => { document.querySelector("header").scrollIntoView(); document.scrollingElement.scrollTop = 0; }); await page.waitForTimeout(300);
    await page.screenshot({ path: `${shots}/oversikt.png` });
    await cards.nth(4).screenshot({ path: `${shots}/temperaturer.png` });
    await page.locator("#learning-card").screenshot({ path: `${shots}/husmodell.png` });
    await page.locator("#hot-water-card").screenshot({ path: `${shots}/varmvatten.png` });
    await cards.nth(1).screenshot({ path: `${shots}/varmeplan.png` });
    await page.locator(".grid").first().screenshot({ path: `${shots}/installningar.png` });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(1200);
    await page.evaluate(() => { document.querySelector("header").scrollIntoView(); document.scrollingElement.scrollTop = 0; }); await page.waitForTimeout(300);
    await page.screenshot({ path: `${shots}/mobil.png` });
    await cards.nth(1).screenshot({ path: `${shots}/mobil-varmeplan.png` });
    await page.locator("#hot-water-card").screenshot({ path: `${shots}/mobil-varmvatten.png` });
    await page.locator("#learning-card").screenshot({ path: `${shots}/mobil-husmodell.png` });
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.waitForTimeout(800);
  }
  await page.locator("#plan-changes").screenshot({ path: `${artifacts}/changes-desktop.png` });
  for (const width of [320, 360, 390, 412]) {
    await page.setViewportSize({ width, height: 844 });
    await page.waitForTimeout(400);
    const wide = await page.evaluate(() => [...document.querySelectorAll("body *")].filter((el) => {
      const box = el.getBoundingClientRect();
      return box.width && box.right > window.innerWidth + 0.5 && !el.closest(".table-scroll");
    }).map((el) => `${el.tagName}#${el.id}.${el.className?.baseVal ?? el.className} ${Math.round(el.getBoundingClientRect().right)}`).slice(0, 15));
    assert.deepEqual(wide, [], `overflow at ${width}px`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `scroll at ${width}px`);
  }
  await page.setViewportSize({ width: 360, height: 800 });
  await page.waitForTimeout(1200);
  await page.locator("#plan-offset").screenshot({ path: `${artifacts}/offset-mobile.png` });
  await page.screenshot({ path: `${artifacts}/dashboard-mobile.png`, fullPage: true });
  // Theme: follows the system when standalone, can be forced, and follows Home Assistant in the ingress iframe.
  const theme = () => page.evaluate(() => document.documentElement.dataset.theme);
  const background = (target) => target.evaluate(() => getComputedStyle(document.body).backgroundColor);
  assert.equal(await theme(), "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  assert.notEqual(await background(page), "rgb(243, 246, 241)");
  if (shots) {
    await page.setViewportSize({ width: 1280, height: 1100 });
    await page.waitForTimeout(1200);
    await page.evaluate(() => { document.querySelector("header").scrollIntoView(); document.scrollingElement.scrollTop = 0; });
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${shots}/morkt-lage.png` });
    await page.locator("main > section.card:not(#comfort-card)").nth(1).screenshot({ path: `${shots}/morkt-varmeplan.png` });
    await page.locator("#hot-water-card").screenshot({ path: `${shots}/morkt-varmvatten.png` });
    await page.locator("#learning-card").screenshot({ path: `${shots}/morkt-husmodell.png` });
    await page.setViewportSize({ width: 1440, height: 1100 });
  }
  await page.locator("#theme-toggle").click();
  assert.equal(await page.locator("#theme-toggle").textContent(), "Tema: ljust");
  assert.equal(await theme(), "light");
  await page.locator("#theme-toggle").click();
  assert.equal(await page.locator("#theme-toggle").textContent(), "Tema: mörkt");
  await page.emulateMedia({ colorScheme: "light" });
  assert.equal(await theme(), "dark");
  await page.reload();
  await page.waitForFunction(() => document.getElementById("plan-curve").querySelector("svg"));
  assert.equal(await theme(), "dark", "Forced theme must persist");
  await page.locator("#theme-toggle").click();
  assert.equal(await page.locator("#theme-toggle").textContent(), "Tema: auto");
  assert.equal(await theme(), "light");
  const haPage = await browser.newPage({ viewport: { width: 420, height: 820 }, colorScheme: "light" });
  haPage.on("pageerror", (error) => errors.push(error.message));
  await haPage.goto(`http://127.0.0.1:${server.address().port}/ha-parent`);
  const panel = haPage.frameLocator("#panel");
  await panel.locator("#curve-details > summary").click();
  await panel.locator("#plan-curve svg").waitFor();
  const frame = haPage.frame({ url: /dashboard-test/ });
  assert.equal(await frame.evaluate(() => document.documentElement.dataset.theme), "dark", "Must follow dark Home Assistant theme");
  await haPage.evaluate(() => { document.documentElement.style.setProperty("--primary-background-color", "#fafafa"); document.body.style.background = "#fafafa"; });
  await frame.waitForFunction(() => document.documentElement.dataset.theme === "light");
  await haPage.close();
  await page.route("**/api/plan", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.offset.changes = [];
    await route.fulfill({ json: data });
  });
  await page.locator("#refresh-plan").click();
  await page.waitForFunction(() => document.getElementById("next-change").textContent === "Ingen behövs");
  assert.equal(await page.locator("#hero-offset").textContent(), "3");
  assert.match(await page.locator("#next-change-why").textContent(), /Ingen ändring av förskjutningen planeras/);
  let statusReads = 0;
  let statusFailure = false;
  await page.route("**/api/status", async (route) => {
    statusReads++;
    if (statusFailure) {
      await route.fulfill({ status: 502, json: { error: "Anslutningen bröts" } });
      return;
    }
    await route.fulfill({ json: {
      indoor: 21.5, outdoor: 9, current_offset: null,
      enabled: true, checked_at: Date.now() / 1000, reading_errors: { current_offset: "Pumpen är otillgänglig" },
      error_message: "Pumpen är otillgänglig",
    } });
  });
  await page.waitForFunction(() => document.getElementById("indoor").textContent === "21,5", null, { timeout: 20000 });
  assert.ok(statusReads > 0, "Status is polled without interaction");
  assert.equal(await page.locator("#hero-offset").textContent(), "—");
  assert.equal(await page.locator("#outdoor-now").textContent(), "9");
  assert.match(await page.locator("#status-message").textContent(), /otillgänglig/);
  statusFailure = true;
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await page.waitForFunction(() => document.getElementById("status-message").textContent === "Anslutningen bröts");
  assert.equal(await page.locator("#indoor").textContent(), "—");
  assert.match(await page.locator("#updated").textContent(), /inte aktuell/);
  await page.unroute("**/api/status");
  await page.route("**/api/status", (route) => route.fulfill({ json: {
    indoor: 21.1, outdoor: 10, current_offset: 3, next_offset: 3, enabled: true,
    checked_at: Date.now() / 1000, control_limit: { direction: "upper", value: 3, requested: 4.35 },
  } }));
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await page.waitForFunction(() => document.getElementById("next-change").textContent === "Övre gränsen nådd");
  assert.match(await page.locator("#next-change-why").textContent(), /4,35.*sparade gräns är 3/);
  assert.equal(await page.locator("#adjust-offset-limits").isVisible(), true);
  await page.locator("#adjust-offset-limits").click();
  assert.equal(await page.locator("#max_offset").isVisible(), true, "Limit warning opens directly to editable limits");
  assert.equal(await page.locator("#max_offset").inputValue(), String(settings.max_offset));
  await page.locator("#max_offset").fill("5");
  await page.locator("#max_offset").dispatchEvent("input");
  assert.match(await page.locator("#settings-save-status").textContent(), /Osparade ändringar.*Högsta förskjutning/);
  assert.equal(settings.max_offset, 3, "Changing a limit is a draft, never automatic");
  await page.locator("#offset-limits button").click();
  await page.waitForFunction(() => document.getElementById("offset-limits-saved").textContent.includes("till 5"));
  assert.equal(settings.max_offset, 5, "New limit is persisted through the ordinary settings endpoint");
  await page.locator("#max_offset").fill("3");
  await page.locator("#offset-limits button").click();
  await page.waitForFunction(() => document.getElementById("offset-limits-saved").textContent.includes("till 3"));
  await page.locator("#refresh-plan").click();
  await page.waitForTimeout(300);
  assert.equal(await page.locator("#next-change").textContent(), "Övre gränsen nådd");
  await page.unroute("**/api/status");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.waitForFunction(() => document.getElementById("hero-offset").textContent === "3");
  await page.waitForFunction(() => document.getElementById("indoor").textContent === "22");
  assert.match(await page.locator("#room-impact-list").textContent(), /Vardagsrum/);
  assert.ok(await page.locator("#room-impact-list .room-area-group").count() >= 1);
  assert.equal(await page.locator("#room_sensors .room-choice-area").filter({ hasText: "Home Assistant-rum: Vardagsrum" }).count(), 1);
  assert.equal(await page.locator("#next-change").textContent(), "Ingen behövs");
  assert.equal(await page.locator("#adjust-offset-limits").isVisible(), false);
  assert.match(await page.locator("#updated").textContent(), /Uppdateras automatiskt/);
  const heavyEndpoints = ["history", "plan", "hotwater", "learning"];
  const heavyReads = Object.fromEntries(heavyEndpoints.map((name) => [name, 0]));
  const heavyActive = { ...heavyReads };
  const heavyPeak = { ...heavyReads };
  let releaseHeavy;
  const heavyGate = new Promise((resolve) => { releaseHeavy = resolve; });
  for (const name of heavyEndpoints) {
    await page.route(`**/api/${name}`, async (route) => {
      heavyReads[name]++;
      heavyActive[name]++;
      heavyPeak[name] = Math.max(heavyPeak[name], heavyActive[name]);
      const response = await route.fetch();
      await heavyGate;
      await route.fulfill({ response });
      heavyActive[name]--;
    });
  }
  await page.evaluate(() => { refreshHistory(); refreshPlan(); refreshHotWater(); refreshLearning(); });
  for (let attempt = 0; attempt < 100 && !heavyEndpoints.every((name) => heavyReads[name] === 1); attempt++) {
    await page.waitForTimeout(20);
  }
  assert.ok(heavyEndpoints.every((name) => heavyReads[name] === 1));
  await page.evaluate(() => {
    for (let i = 0; i < 8; i++) {
      refreshHistory(); refreshPlan(); refreshHotWater(); refreshLearning();
    }
  });
  await page.waitForTimeout(100);
  assert.ok(heavyEndpoints.every((name) => heavyReads[name] === 1), "Burst refreshes must not duplicate in-flight requests");
  releaseHeavy();
  for (let attempt = 0; attempt < 100 && !heavyEndpoints.every((name) => heavyReads[name] === 2 && heavyActive[name] === 0); attempt++) {
    await page.waitForTimeout(20);
  }
  assert.ok(heavyEndpoints.every((name) => heavyReads[name] === 2), "Coalesce refreshes into one fresh follow-up, not a queue of eight");
  assert.ok(heavyEndpoints.every((name) => heavyPeak[name] === 1), "Each expensive endpoint allows only one in-flight request");
  for (const name of heavyEndpoints) await page.unroute(`**/api/${name}`);
  failHistory = true;
  await page.locator("#refresh-history").click();
  await page.waitForFunction(() => document.getElementById("history-status").textContent.includes("Recorder saknas"));
  assert.equal(await page.locator("#temperature-chart svg").count(), 0);
  assert.equal(await page.locator("#enabled").isChecked(), true, "History failure must not change automation");
  const nordpoolPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  nordpoolPage.on("pageerror", (error) => errors.push(error.message));
  const nordpoolId = "sensor.nord_pool_se3_aktuellt_pris";
  const nordpoolSettings = { ...settings, price_provider: "tibber", price_entity: price, preheat_enabled: false };
  let providerWrites = 0;
  nordpoolPage.on("dialog", (dialog) => dialog.accept());
  await nordpoolPage.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      Object.assign(nordpoolSettings, route.request().postDataJSON());
      providerWrites++;
    }
    return route.fulfill({ json: nordpoolSettings });
  });
  await nordpoolPage.route("**/api/entities", (route) => route.fulfill({ json: [
    ...entities, { entity_id: nordpoolId, name: "Nord Pool SE3 Aktuellt pris", state: "0.5", unit: "SEK/kWh" },
  ] }));
  await nordpoolPage.route("**/api/discovery", (route) => route.fulfill({ json: {
    offset_entity: offset, outdoor_entity: outdoor, price_entity: price,
    message: "NIBE hittad", price_message: "Tibbers aktuella elpris hittades automatiskt.",
    price_sources: {
      tibber: { price_entity: price, price_message: "Tibbers aktuella elpris hittades automatiskt." },
      nordpool: { price_entity: nordpoolId, price_message: "Nord Pools aktuella elpris hittades automatiskt." },
    },
  } }));
  await nordpoolPage.route("**/api/overview", (route) => route.fulfill({ json: nordpoolSettings.price_provider === "nordpool"
    ? { ...overview, house: [{ label: "Husets effekt just nu", value: null, error: "Ingen förbrukningsmätare vald." }],
      price: { label: "Aktuellt elpris · Nord Pool", value: "0.5", unit: "SEK/kWh" },
      electricity_note: "Elpriset kommer från Nord Pool. Enegics förbrukningsmätning är separat." }
    : overview }));
  await nordpoolPage.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await nordpoolPage.waitForFunction((id) => document.getElementById("price_entity").value === id, price);
  assert.equal(await nordpoolPage.locator("#price_provider").inputValue(), "tibber");
  assert.match(await nordpoolPage.locator("#price-provider-status").textContent(), /Sparad elpriskälla: Tibber/);
  assert.equal(await nordpoolPage.locator("#nordpool-help").isVisible(), false);
  await nordpoolPage.locator("#settings-panel > summary").click();
  await nordpoolPage.locator("#price_provider").selectOption("nordpool");
  await nordpoolPage.waitForFunction((id) => document.getElementById("price_entity").value === id, nordpoolId);
  assert.match(await nordpoolPage.locator("#price-provider-status").textContent(), /Osparat val: Nord Pool/);
  assert.equal(providerWrites, 0, "Source change is only a draft until explicitly saved");
  nordpoolSettings.min_interval_minutes = 45;
  await nordpoolPage.evaluate(() => refreshSavedSettings());
  assert.equal(await nordpoolPage.locator("#price_provider").inputValue(), "nordpool", "External settings polling preserves the source draft");
  assert.equal(await nordpoolPage.locator("#price_entity").inputValue(), nordpoolId, "Source and sensor drafts remain paired");
  assert.match(await nordpoolPage.locator("#price-discovery").textContent(), /Nord Pool/);
  assert.doesNotMatch(await nordpoolPage.locator("#price-provider-status").textContent(), /Tibber/);
  assert.match(await nordpoolPage.locator("#price-unit").textContent(), /SEK\/kWh/);
  assert.equal(await nordpoolPage.locator("#preheat_enabled").isChecked(), false, "Discovering Nord Pool never activates preheating");
  assert.match(await nordpoolPage.locator("#manual-price").locator("..").textContent(), /Enegic mäter förbrukning/);
  await nordpoolPage.locator("#save-near").click();
  await nordpoolPage.waitForFunction(() => document.getElementById("price-provider-status").textContent.includes("Sparad elpriskälla: Nord Pool"));
  assert.equal(providerWrites, 1);
  assert.equal(nordpoolSettings.price_provider, "nordpool");
  assert.equal(nordpoolSettings.price_entity, nordpoolId);
  await nordpoolPage.waitForFunction(() => document.getElementById("electricity-note").textContent.includes("Nord Pool"));
  assert.doesNotMatch(await nordpoolPage.locator("#electricity-metrics").textContent(), /Tibber/);
  assert.equal(await nordpoolPage.locator("#nordpool-help").isVisible(), true);
  await nordpoolPage.reload();
  await nordpoolPage.waitForFunction(() => document.getElementById("price_provider").value === "nordpool");
  assert.equal(await nordpoolPage.locator("#price_entity").inputValue(), nordpoolId, "Saved Nord Pool selection survives reload with Tibber present");
  await nordpoolPage.locator("#settings-panel > summary").click();
  await nordpoolPage.locator("#price_provider").selectOption("tibber");
  await nordpoolPage.waitForFunction((id) => document.getElementById("price_entity").value === id, price);
  assert.match(await nordpoolPage.locator("#price-provider-status").textContent(), /Osparat val: Tibber/);
  assert.equal(providerWrites, 1, "Switching back to Tibber also requires saving");
  await nordpoolPage.close();
  const priorityPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
  priorityPage.on("pageerror", (error) => errors.push(error.message));
  const thirdRoom = entities.find((entity) => entity.name.startsWith("LK Badrum")).entity_id;
  let comfortMode = "heat";
  let comfortSetpoint = 20;
  let comfortEvents = [{ id: 100, kind: "offset_observed", timestamp: now - 7200, message: "Ändring",
    details: { entity_id: offset, previous: 3, value: 4 } }];
  await priorityPage.route("**/api/events*", (route) => route.fulfill({ json: comfortEvents }));
  let priorityConfig = { ...settings, target_temperature: 22, room_sensors: ["sensor.living", thermostat, thirdRoom], priority_sensors: [] };
  await priorityPage.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") priorityConfig = route.request().postDataJSON();
    await route.fulfill({ json: priorityConfig });
  });
  await priorityPage.route("**/api/status", (route) => route.fulfill({ json: {
    indoor: priorityConfig.priority_sensors.length === 1 ? 21 : priorityConfig.priority_sensors.length === 2 ? 22 : 23,
    house_indoor: 23, current_offset: 2, next_offset: 3, outdoor: 10, checked_at: Date.now() / 1000, enabled: true,
    warm_room_limit: 24,
    warm_rooms: priorityConfig.priority_sensors.length ? [thirdRoom] : [],
    rooms: [
      { entity_id: "sensor.living", name: "Vardagsrum", temperature: 21,
        thermostat: "climate.living", thermostat_mode: comfortMode, setpoint: comfortSetpoint },
      { entity_id: thermostat, name: "LK Allrum", temperature: 23 },
      { entity_id: thirdRoom, name: entities.find((entity) => entity.entity_id === thirdRoom).name, temperature: 25 },
    ].map((room) => ({ ...room, priority: priorityConfig.priority_sensors.includes(room.entity_id) })),
  } }));
  await priorityPage.route("**/api/history", (route) => route.fulfill({ json: {
    start: now - 86400, end: now, target: 22,
    series: priorityConfig.room_sensors.map((id) => ({
      entity_id: id, name: id, points: [[now - 7200, id === "sensor.living" ? 20 : id === thermostat ? 22 : 24], [now, id === "sensor.living" ? 21 : id === thermostat ? 23 : 25]],
    })),
    changes: [{ id: 501, timestamp: now - 3600, kind: "thermostat", entity_id: thermostat,
      status: "confirmed", value: 22, previous: 23, indoor: 23, target: 22 }],
  } }));
  await priorityPage.route("**/api/areas", (route) => route.fulfill({ json: Object.fromEntries(
    priorityConfig.room_sensors.map((id) => [id, areaMap[id] || { area_id: null, area_name: null }]),
  ) }));
  await priorityPage.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await priorityPage.locator("#room-impact-list .room-impact-row").first().waitFor({ state: "attached" });
  assert.equal(await priorityPage.locator("#indoor").textContent(), "23");
  assert.equal(await priorityPage.locator("#house-indoor").textContent(), "23");
  assert.equal(await priorityPage.locator("#warm-room-warning").isVisible(), false);
  await priorityPage.locator("#settings-panel > summary").click();
  await priorityPage.locator('#priority_sensors input[value="sensor.living"]').check();
  priorityPage.once("dialog", (dialog) => dialog.accept());
  await priorityPage.locator("#save").click();
  await priorityPage.waitForFunction(() => document.getElementById("indoor").textContent === "21");
  assert.deepEqual(priorityConfig.priority_sensors, ["sensor.living"]);
  assert.equal(await priorityPage.locator("#indoor-label").textContent(), "Prioriterade rum nu");
  assert.equal(await priorityPage.locator("#house-indoor").textContent(), "23");
  assert.equal(await priorityPage.locator("#room-impact").getAttribute("open"), "");
  await priorityPage.waitForFunction(() => !document.getElementById("history-status").textContent.startsWith("Hämtar"));
  assert.match(await priorityPage.locator("#room-impact-list").textContent(), /\+1 °C senaste timmen/, await priorityPage.locator("#history-status").textContent());
  assert.match(await priorityPage.locator("#room-impact-list").textContent(), /Vardagsrum · prioriterat.*21 °C · 1° under målet/);
  assert.equal(await priorityPage.locator("#room-impact-list .room-area-group").count(), 3);
  assert.match(await priorityPage.locator("#history-area-status").textContent(), /rumstilldelningen i Home Assistant/);
  const allrumCard = priorityPage.locator(".thermostat-history").filter({ hasText: "Allrum" });
  assert.match(await allrumCard.textContent(), /LK Allrum.*Övertemperaturbegränsning/);
  assert.match(await allrumCard.locator(".room-history-changes").textContent(), /Övertemperaturbegränsning/);
  assert.match(await priorityPage.locator("#room-impact-list").textContent(), /25 °C · 3° över målet/);
  assert.match(await priorityPage.locator("#comfort-advice").textContent(), /Vardagsrum.*inställd på 20 °C.*begränsa värmen/);
  assert.match(await priorityPage.locator("#comfort-advice").textContent(), /Andra rum är redan minst 2 °C/);
  assert.match(await priorityPage.locator("#comfort-change").textContent(), /3 → 4.*Jämför uppmätta/);
  assert.match(await priorityPage.locator("#comfort-response").textContent(), /Vardagsrum: 20 → 21 °C \(\+1 °C sedan höjningen\)/);
  await priorityPage.locator("#comfort-card").screenshot({ path: `${artifacts}/comfort-card.png` });
  comfortMode = "off";
  await priorityPage.evaluate(() => window.dispatchEvent(new Event("online")));
  await priorityPage.waitForFunction(() => document.getElementById("comfort-advice").textContent.includes("Termostaten är avstängd"));
  comfortMode = "heat";
  comfortSetpoint = null;
  await priorityPage.evaluate(() => window.dispatchEvent(new Event("online")));
  await priorityPage.waitForFunction(() => document.getElementById("comfort-advice").textContent.includes("börvärde saknas"));
  comfortSetpoint = 22;
  comfortEvents = [{ ...comfortEvents[0], timestamp: now - 600 }];
  await priorityPage.locator("#log-panel > summary").click();
  await priorityPage.locator("#refresh-events").click();
  await priorityPage.evaluate(() => window.dispatchEvent(new Event("online")));
  await priorityPage.waitForFunction(() => document.getElementById("comfort-change").textContent.includes("tidigt att bedöma"));
  await priorityPage.waitForFunction(() => document.getElementById("comfort-advice").textContent.includes("Det visar inte att ventilen är öppen"));
  comfortEvents = [{ ...comfortEvents[0], details: { entity_id: offset, previous: 4, value: 3 } }];
  await priorityPage.locator("#refresh-events").click();
  await priorityPage.evaluate(() => window.dispatchEvent(new Event("online")));
  await priorityPage.waitForFunction(() => document.getElementById("comfort-change").textContent.includes("en sänkning"));
  assert.equal(await priorityPage.locator("#comfort-response li").count(), 0);
  comfortEvents = [{ ...comfortEvents[0], details: { entity_id: "number.another_pump", previous: 3, value: 4 } }];
  await priorityPage.locator("#refresh-events").click();
  await priorityPage.evaluate(() => window.dispatchEvent(new Event("online")));
  await priorityPage.waitForFunction(() => document.getElementById("comfort-change").textContent.includes("Ingen bekräftad höjning"));
  comfortEvents = [{ id: 101, kind: "offset_observed", timestamp: now - 2 * 86400,
    details: { entity_id: offset, previous: 3, value: 4 } }];
  await priorityPage.locator("#refresh-events").click();
  await priorityPage.waitForFunction(() => document.getElementById("comfort-response").textContent.includes("jämförelse saknas"));
  assert.doesNotMatch(await priorityPage.locator("#comfort-response").textContent(), /sedan höjningen/);
  await priorityPage.unroute("**/api/events*");
  await priorityPage.route("**/api/events*", (route) => route.fulfill({
    status: 502, json: { error: "Loggen kan inte nås" },
  }));
  await priorityPage.locator("#refresh-events").click();
  await priorityPage.waitForFunction(() => document.getElementById("comfort-change").textContent.includes("Loggen kan inte nås"));
  assert.equal(await priorityPage.locator("#comfort-response li").count(), 0);
  assert.equal(await priorityPage.locator("#warm-room-warning").isVisible(), true);
  assert.match(await priorityPage.locator("#warm-room-summary").textContent(), /24 °C.*planerar ändå att höja/);
  assert.match(await priorityPage.locator("#warm-room-list").textContent(), /LK Badrum.*25 °C.*3° över målet.*\+1 °C senaste timmen/);
  assert.equal(await priorityPage.locator("#room-impact-list .too-warm").count(), 1);
  await priorityPage.locator(`#priority_sensors input[value="${thermostat}"]`).check();
  assert.equal(await priorityPage.locator(`#priority_sensors input[value="${thirdRoom}"]`).isDisabled(), true);
  priorityPage.once("dialog", (dialog) => dialog.accept());
  await priorityPage.locator("#save").click();
  await priorityPage.waitForFunction(() => document.getElementById("indoor").textContent === "22");
  assert.deepEqual([...priorityConfig.priority_sensors].sort(), ["sensor.living", thermostat].sort());
  await priorityPage.locator(".hero").screenshot({ path: `${artifacts}/priority-rooms.png` });
  for (const width of [320, 390]) {
    await priorityPage.setViewportSize({ width, height: 844 });
    assert.equal(await priorityPage.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `Priority rooms overflow at ${width}px`);
  }
  await priorityPage.locator(`#room_sensors input[value="${thermostat}"]`).uncheck();
  assert.equal(await priorityPage.locator(`#priority_sensors input[value="${thermostat}"]`).count(), 0);
  assert.equal(await priorityPage.locator(`#priority_sensors input[value="${thirdRoom}"]`).isDisabled(), false);
  await priorityPage.locator('#priority_sensors input[value="sensor.living"]').uncheck();
  priorityPage.once("dialog", (dialog) => dialog.accept());
  await priorityPage.locator("#save").click();
  await priorityPage.waitForFunction(() => document.getElementById("indoor-label").textContent === "Inne nu");
  assert.deepEqual(priorityConfig.priority_sensors, []);
  assert.equal(await priorityPage.locator("#warm-room-warning").isVisible(), false);
  await priorityPage.close();
  const thermostatPage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  thermostatPage.on("pageerror", (error) => errors.push(error.message));
  let thermostatSettings = { ...settings, room_sensors: [thermostat, thirdRoom], priority_sensors: [],
    nearest_thermostat: "", thermostat_control_enabled: false };
  let thermostatSaves = 0;
  let thermostatSaveFails = false;
  let thermostatHistoryFails = false;
  await thermostatPage.route("**/api/history", (route) => {
    if (thermostatHistoryFails) return route.fulfill({ status: 502, json: { error: "Ändringsloggen kan inte läsas" } });
    return route.fulfill({ json: {
      ...history(),
      series: [thermostat, thirdRoom].map((id) => ({ entity_id: id,
        name: entities.find((e) => e.entity_id === id).name, kind: "climate",
        points: [[now - 86400, 21], [now - 7200, 22], [now - 60, 23]],
        setpoints: [[now - 86400, 24], [now - 3600, 22]] })),
      changes: [
        { id: 50, timestamp: now - 7200, kind: "offset", entity_id: offset, status: "confirmed",
          previous: 2, value: 3, contributions: { room: 1.5, weather: 0, price: -0.5 },
          indoor: 21, outdoor: 4, price: 2.5, target: 22, outcome_at: now - 7100 },
        { id: 51, timestamp: now - 3600, kind: "thermostat", entity_id: thermostat, status: "confirmed",
          previous: 24, value: 22, unit: "°C", indoor: 23, target: 22 },
        { id: 52, timestamp: now - 1800, kind: "thermostat", entity_id: thirdRoom, status: "unconfirmed",
          previous: 25, value: 22, unit: "°C", indoor: 24, target: 22 },
      ],
    } });
  });
  await thermostatPage.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      if (thermostatSaveFails) return route.fulfill({ status: 400, json: { error: "Termostaten är otillgänglig" } });
      thermostatSettings = route.request().postDataJSON();
      thermostatSaves++;
    }
    return route.fulfill({ json: thermostatSettings });
  });
  await thermostatPage.route("**/api/status", (route) => route.fulfill({ json: {
    indoor: 23, house_indoor: 23, checked_at: Date.now() / 1000, enabled: true,
    rooms: [{ entity_id: thermostat, name: "LK Allrum", temperature: 23, setpoint: 22,
      nearest: thermostatSettings.nearest_thermostat === thermostat }],
    thermostat_control: { enabled: thermostatSettings.thermostat_control_enabled, rooms: [
      { entity_id: thermostat, message: "Börvärdet är redan vid eller under målet; höjs inte." },
    ] },
  } }));
  await thermostatPage.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await thermostatPage.locator(`#room_sensors input[value="${thermostat}"]`).waitFor({ state: "attached" });
  await thermostatPage.waitForFunction(() => document.querySelectorAll("#thermostat-charts svg").length === 2);
  await thermostatPage.locator("#thermostat-charts").screenshot({ path: `${artifacts}/thermostat-history.png` });
  assert.equal(await thermostatPage.locator("#temperature-chart .chart-event").count(), 3);
  assert.equal(await thermostatPage.locator(".thermostat-history").first().locator(".chart-event").count(), 1);
  assert.equal(await thermostatPage.locator(".thermostat-history").last().locator(".chart-event").count(), 1);
  for (const svg of await thermostatPage.locator("#thermostat-charts svg").all()) {
    const expected = await svg.evaluate((element) => Math.round(element.parentElement.clientWidth));
    const width = Number((await svg.getAttribute("viewBox")).split(" ")[2]);
    assert.ok(Math.abs(expected - width) <= 1, "Every thermostat graph must be sized after the grid has settled");
  }
  assert.match(await thermostatPage.locator("#history-changes").textContent(), /rum 1,5, utetemperatur 0, elpris \u22120,5/);
  assert.match(await thermostatPage.locator("#history-changes").textContent(), /återläsning saknas/);
  assert.equal(await thermostatPage.locator(".thermostat-history path.dashed").count(), 2);
  await thermostatPage.locator("#history-entity").selectOption(thermostat);
  assert.equal(await thermostatPage.locator("#thermostat-charts svg").count(), 1);
  assert.equal(await thermostatPage.locator("#history-changes li").count(), 2);
  assert.doesNotMatch(await thermostatPage.locator("#history-changes").textContent(), /återläsning saknas/);
  await thermostatPage.locator("#history-entity").selectOption("");
  thermostatHistoryFails = true;
  await thermostatPage.locator("#refresh-history").click();
  await thermostatPage.waitForFunction(() => document.getElementById("history-status").textContent.includes("kan inte läsas"));
  assert.equal(await thermostatPage.locator("#thermostat-charts svg").count(), 0);
  assert.equal(await thermostatPage.locator("#history-changes li").count(), 0);
  thermostatHistoryFails = false;
  await thermostatPage.locator("#refresh-history").click();
  await thermostatPage.waitForFunction(() => document.querySelectorAll("#thermostat-charts svg").length === 2);
  await thermostatPage.locator("#settings-panel > summary").click();
  assert.equal(await thermostatPage.locator('#room_sensors input[value="sensor.living"]').isVisible(), false);
  assert.equal(await thermostatPage.locator(`#room_sensors input[value="${thermostat}"]`).isVisible(), true);
  assert.match(await thermostatPage.locator("#room_sensors").textContent(), /inställd 26/);
  await thermostatPage.locator("#room-search").fill("badrum");
  assert.equal(await thermostatPage.locator(`#room_sensors input[value="${thermostat}"]`).isVisible(), false);
  assert.equal(await thermostatPage.locator(`#room_sensors input[value="${thirdRoom}"]`).isVisible(), true);
  await thermostatPage.locator("#room-search").fill("");
  await thermostatPage.locator("#show-room-sensors").check();
  assert.equal(await thermostatPage.locator('#room_sensors input[value="sensor.living"]').isVisible(), true);
  await thermostatPage.locator('#room_sensors input[value="sensor.living"]').check();
  await thermostatPage.locator('#priority_sensors input[value="sensor.living"]').check();
  await thermostatPage.locator("#nearest_thermostat").selectOption(thermostat);
  await thermostatPage.locator("#room-search").fill("badrum");
  assert.equal(await thermostatPage.locator('#room_sensors input[value="sensor.living"]').isVisible(), false);
  assert.match(await thermostatPage.locator("#selected-room-sensors").textContent(), /sensor\.living/);
  assert.match(await thermostatPage.locator("#room-selection-count").textContent(), /2 termostater \+ 1 separata givare/);
  await thermostatPage.locator("#thermostat_control_enabled").check();
  await thermostatPage.locator("#save").click();
  assert.equal(thermostatSaves, 0, "Mixed selection must be explained before consent or saving");
  assert.match(await thermostatPage.locator("#notice").textContent(), /sensor\.living/);
  assert.equal(await thermostatPage.locator('#room_sensors input[value="sensor.living"]').isVisible(), true);
  await thermostatPage.locator("#remove-room-sensors").click();
  assert.equal(await thermostatPage.locator('#room_sensors input[value="sensor.living"]').isChecked(), false);
  assert.equal(await thermostatPage.locator('#priority_sensors input[value="sensor.living"]').count(), 0);
  assert.equal(await thermostatPage.locator(`#room_sensors input[value="${thermostat}"]`).isChecked(), true);
  assert.equal(await thermostatPage.locator(`#room_sensors input[value="${thirdRoom}"]`).isChecked(), true);
  assert.equal(await thermostatPage.locator("#nearest_thermostat").inputValue(), thermostat);
  assert.equal(thermostatSaves, 0, "Explicit removal must only edit the form, not save it");
  await thermostatPage.locator("#thermostat_control_enabled").uncheck();
  await thermostatPage.locator("#show-room-sensors").uncheck();
  await thermostatPage.locator("#nearest_thermostat").selectOption(thermostat);
  await thermostatPage.locator("#thermostat_control_enabled").check();
  assert.match(await thermostatPage.locator("#settings-save-status").textContent(), /Osparade ändringar.*Termostatbegränsning/);
  thermostatPage.once("dialog", (dialog) => dialog.dismiss());
  await thermostatPage.locator("#save-near").click();
  assert.equal(thermostatSaves, 0, "Rejecting consent must not save or enable thermostat writes");
  thermostatPage.once("dialog", (dialog) => dialog.accept());
  await thermostatPage.locator("#save-near").click();
  await thermostatPage.waitForFunction(() => document.getElementById("notice").textContent.includes("sparade"));
  assert.equal(thermostatSettings.nearest_thermostat, thermostat);
  assert.equal(thermostatSettings.thermostat_control_enabled, true);
  assert.equal(thermostatSettings.target_temperature, settings.target_temperature);
  assert.match(await thermostatPage.locator("#settings-save-status").textContent(), /sparade/);
  await thermostatPage.waitForFunction(() => document.getElementById("room-impact-list").textContent.includes("närmast pumpen"));
  assert.match(await thermostatPage.locator("#thermostat-control-status").textContent(), /Termostatbegränsning på/);
  await thermostatPage.locator("#room_sensors").screenshot({ path: `${artifacts}/thermostat-picker.png` });
  await thermostatPage.emulateMedia({ colorScheme: "dark" });
  for (const width of [320, 390, 1280]) {
    await thermostatPage.setViewportSize({ width, height: 900 });
    await thermostatPage.locator("#price_low").scrollIntoViewIfNeeded();
    const bar = await thermostatPage.locator(".settings-savebar").boundingBox();
    assert.ok(bar.y >= -1 && bar.y + bar.height <= 900, `Save bar must remain visible at ${width}px`);
    assert.equal(await thermostatPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `Thermostat picker overflow at ${width}`);
  }
  await thermostatPage.locator("#thermostat_control_enabled").uncheck();
  await thermostatPage.locator(`#room_sensors input[value="${thermostat}"]`).uncheck();
  assert.equal(await thermostatPage.locator("#nearest_thermostat").inputValue(), "");
  thermostatSaveFails = true;
  await thermostatPage.locator("#save-near").click();
  await thermostatPage.waitForFunction(() => document.getElementById("settings-save-status").textContent.includes("otillgänglig"));
  assert.equal(await thermostatPage.locator("#thermostat_control_enabled").isChecked(), false);
  assert.equal(thermostatSettings.thermostat_control_enabled, true, "Failed save must not update persisted settings");
  thermostatSaveFails = false;
  await thermostatPage.locator("#save-near").click();
  await thermostatPage.waitForFunction(() => !document.getElementById("save").disabled);
  assert.equal(thermostatSettings.thermostat_control_enabled, false);
  assert.equal(thermostatSettings.nearest_thermostat, "");
  assert.equal(thermostatSettings.room_sensors.includes(thermostat), false);
  const savesBeforeDisclosure = thermostatSaves;
  await thermostatPage.locator("#settings-panel").evaluate((element) => { element.open = false; });
  await thermostatPage.evaluate(() => {
    document.getElementById("max_offset").value = "99";
    document.getElementById("settings").requestSubmit();
  });
  assert.equal(await thermostatPage.locator("#max_offset").isVisible(), true, "Invalid hidden fields are revealed before focus");
  assert.equal(thermostatSaves, savesBeforeDisclosure);
  await thermostatPage.locator("#max_offset").fill(String(thermostatSettings.max_offset));
  await thermostatPage.route("**/api/plan", (route) => route.fulfill({ status: 502, json: { error: "Planunderlag saknas" } }), { times: 1 });
  await thermostatPage.locator("#refresh-plan").click();
  await thermostatPage.waitForFunction(() => document.getElementById("plan-status").textContent.includes("Planunderlag saknas"));
  await thermostatPage.locator("#plan-details > summary").click();
  await thermostatPage.waitForTimeout(50);
  assert.equal(await thermostatPage.locator("#plan-details svg").count(), 0, "Opening details must not resurrect cleared stale charts");
  assert.equal(thermostatSaves, savesBeforeDisclosure);
  await thermostatPage.close();
  const livePage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  livePage.on("pageerror", (error) => errors.push(error.message));
  await livePage.clock.install({ time: new Date(now * 1000) });
  let liveSettings = { ...settings, target_temperature: 21, room_sensors: [thermostat],
    priority_sensors: [thermostat], nearest_thermostat: thermostat };
  let liveIndoor = 21, liveOutdoor = 7, liveHouse = 21.2;
  let statusFails = false, settingsFail = false, staleReadings = false;
  let delayNextSettings = false, releaseSettings;
  let pendingWrite = null;
  let liveWrites = 0;
  let liveEntityTemperature = 22.2;
  let entityReads = 0;
  await livePage.route("**/api/entities", (route) => {
    entityReads++;
    return route.fulfill({ json: entities.map((e) => e.entity_id === thermostat
      ? { ...e, current_temperature: liveEntityTemperature } : e) });
  });
  await livePage.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") {
      liveWrites++;
      liveSettings = route.request().postDataJSON();
    }
    const captured = structuredClone(liveSettings);
    if (delayNextSettings && route.request().method() === "GET") {
      delayNextSettings = false;
      await new Promise((resolve) => { releaseSettings = resolve; });
    }
    return route.fulfill(settingsFail ? { status: 502, json: { error: "Inställningskontakt saknas" } }
      : { json: captured });
  });
  await livePage.route("**/api/status", async (route) => route.fulfill(statusFails
    ? { status: 502, json: { error: "Tillfälligt statusfel" } }
    : { json: {
      checked_at: await livePage.evaluate(() => Date.now() / 1000) - (staleReadings ? 600 : 0), enabled: true, indoor: liveIndoor,
      outdoor: liveOutdoor, house_indoor: liveHouse, current_offset: 3,
      next_offset: 3, desired_offset: 3, wait_seconds: pendingWrite ? 600 : 0,
      pending_write: pendingWrite, message: "Ingen ändring behövs.",
    } }));
  await livePage.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await livePage.waitForFunction(() => document.getElementById("indoor").textContent === "21");
  liveSettings = { ...liveSettings, target_temperature: 22 };
  liveIndoor = 21.4; liveOutdoor = 5; liveHouse = 21.6;
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("outdoor-now").textContent === "5"
    && document.getElementById("target_temperature").value === "22");
  assert.equal(await livePage.locator("#indoor").textContent(), "21,4");
  assert.equal(await livePage.locator("#house-indoor").textContent(), "21,6");
  assert.equal(await livePage.locator("#target_temperature").inputValue(), "22");
  await livePage.locator("#target_temperature").fill("24");
  await livePage.locator("#settings-panel > summary").click();
  await livePage.locator("#manual-price").evaluate((node) => { node.open = true; });
  await livePage.locator("#price_entity").selectOption(outdoor);
  await livePage.locator(`#priority_sensors input[value="${thermostat}"]`).uncheck();
  liveSettings = { ...liveSettings, target_temperature: 20, min_interval_minutes: 45,
    room_sensors: ["sensor.living"], priority_sensors: [], nearest_thermostat: "" };
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("settings-sync-status").textContent.includes("Sparat mål: 20"));
  assert.equal(await livePage.locator("#target_temperature").inputValue(), "24", "Polling preserves the unsaved target");
  assert.equal(await livePage.locator("#price_entity").inputValue(), outdoor, "Polling preserves unsaved selectors");
  assert.equal(await livePage.locator(`#room_sensors input[value="${thermostat}"]`).isChecked(), true);
  assert.equal(await livePage.locator(`#priority_sensors input[value="${thermostat}"]`).isChecked(), false);
  assert.equal(await livePage.locator("#nearest_thermostat").inputValue(), thermostat, "Coupled room drafts stay together");
  liveEntityTemperature = 23.4;
  await livePage.clock.fastForward(61000);
  await livePage.waitForFunction(() => [...document.querySelectorAll("#room_sensors label")]
    .some((label) => label.textContent.includes("23,4")));
  assert.ok(entityReads >= 2, "Sensor values in settings refresh without leaving the page");
  assert.equal(await livePage.locator("#target_temperature").inputValue(), "24", "Refreshing entities preserves unsaved target");
  assert.equal(await livePage.locator("#price_entity").inputValue(), outdoor, "Refreshing entities preserves unsaved sensor choice");
  assert.equal(await livePage.locator("#min_interval_minutes").inputValue(), "45", "Unedited settings follow external changes");
  assert.match(await livePage.locator("#settings-save-status").textContent(), /Osparade.*Måltemperatur/);
  settingsFail = true;
  liveOutdoor = 4;
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("outdoor-now").textContent === "4"
    && document.getElementById("settings-sync-status").textContent.includes("Inställningskontakt saknas"));
  assert.match(await livePage.locator("#settings-sync-status").textContent(), /Inställningskontakt saknas/);
  settingsFail = false;
  statusFails = true;
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("status-message").textContent === "Tillfälligt statusfel");
  assert.equal(await livePage.locator("#indoor").textContent(), "—");
  assert.equal(await livePage.locator("#target_temperature").inputValue(), "24");
  statusFails = false;
  liveIndoor = 21.5;
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("indoor").textContent === "21,5");
  staleReadings = true;
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("status-title").textContent === "Väntar på aktuell avläsning");
  assert.equal(await livePage.locator("#outdoor-now").textContent(), "—");
  assert.equal(await livePage.locator("#house-indoor").textContent(), "—");
  staleReadings = false;
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("outdoor-now").textContent === "4");
  assert.equal(liveWrites, 0, "Background refresh must never save settings or issue control commands");
  pendingWrite = { error: "Home Assistant kunde inte nås" };
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("next-change").textContent === "Återförsök väntar");
  assert.match(await livePage.locator("#next-change-why").textContent(), /Återläser.*10 min.*fortfarande behövs/);
  pendingWrite = null;
  delayNextSettings = true;
  liveOutdoor = 2;
  await livePage.clock.fastForward(16000);
  await livePage.waitForFunction(() => document.getElementById("outdoor-now").textContent === "2");
  assert.equal(typeof releaseSettings, "function", "Status refresh does not wait for a slow settings request");
  livePage.on("dialog", (dialog) => dialog.accept());
  await livePage.locator("#save-target").click();
  await livePage.waitForFunction(() => document.getElementById("notice").textContent.includes("Inställningarna är sparade"));
  assert.match(await livePage.locator("#settings-sync-status").textContent(), /Sparat mål: 24 °C/);
  const oldSettingsResponse = livePage.waitForResponse((response) => response.url().endsWith("/api/settings")
    && response.request().method() === "GET");
  releaseSettings();
  await oldSettingsResponse;
  assert.equal(await livePage.locator("#target_temperature").inputValue(), "24", "An old in-flight read cannot overwrite a successful save");
  assert.equal(liveWrites, 1, "Only the explicit save may write settings");
  let heldStatus;
  const heldGate = new Promise((resolve) => { heldStatus = resolve; });
  let statusStarted = false;
  await livePage.route("**/api/status", async (route) => {
    statusStarted = true;
    await heldGate;
    await route.fulfill({ json: { checked_at: Date.now() / 1000, indoor: 10 } });
  }, { times: 1 });
  await livePage.evaluate(() => void refreshStatus());
  for (let attempt = 0; attempt < 50 && !statusStarted; attempt++) await page.waitForTimeout(20);
  assert.ok(statusStarted);
  liveIndoor = 21.7;
  await livePage.evaluate(() => window.dispatchEvent(new Event("focus")));
  await livePage.waitForFunction(() => document.getElementById("indoor").textContent === "21,7");
  heldStatus();
  await page.waitForTimeout(100);
  assert.equal(await livePage.locator("#indoor").textContent(), "21,7", "Resuming cancels stalled reads and ignores their late response");
  await livePage.close();
  const firePage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  firePage.on("pageerror", (error) => errors.push(error.message));
  firePage.on("dialog", (dialog) => dialog.accept());
  let fireSettings = { ...defaults, room_sensors: [thermostat, "sensor.living"], priority_sensors: [thermostat],
    offset_entity: offset, fireplace_enabled: false, fireplace_rooms: [] };
  let fireWrites = 0;
  let fireObservation = { enabled: false, phase: "off", message: "Kaminobservation är av.",
    completed: 0, learned: false, room_effects: [], recent: [], rooms: [], active_since: null };
  await firePage.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      fireWrites++;
      fireSettings = route.request().postDataJSON();
    }
    return route.fulfill({ json: fireSettings });
  });
  await firePage.route("**/api/status", (route) => route.fulfill({ json: {
    checked_at: Date.now() / 1000, enabled: false, indoor: 23, house_indoor: 22, outdoor: 5,
    current_offset: 3, next_offset: 2, desired_offset: 2, fireplace: fireObservation,
    rooms: [{ entity_id: thermostat, name: "LK Allrum", temperature: 23, priority: true }],
  } }));
  await firePage.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await firePage.waitForFunction(() => document.getElementById("fireplace-status").textContent === "Kaminobservation är av.");
  assert.equal(await firePage.locator("#fireplace_enabled").isChecked(), false);
  await firePage.locator("#fireplace_enabled").check();
  await firePage.locator(`#fireplace_rooms input[value="${thermostat}"]`).check();
  assert.match(await firePage.locator("#settings-save-status").textContent(), /Osparade.*Kaminanpassning.*Kaminrum/);
  assert.equal(fireWrites, 0, "Selecting a fireplace must not silently save or enable automation");
  await firePage.locator("#save-fireplace").click();
  await firePage.waitForFunction(() => document.getElementById("notice").textContent.includes("Inställningarna är sparade"));
  assert.equal(fireWrites, 1);
  assert.equal(fireSettings.fireplace_enabled, true);
  assert.deepEqual(fireSettings.fireplace_rooms, [thermostat]);
  assert.deepEqual(fireSettings.priority_sensors, [thermostat], "The chosen priority rooms must stay unchanged");
  assert.equal(fireSettings.enabled, false, "Observations do not turn on physical control");
  fireObservation = { ...fireObservation, enabled: true, rooms: [thermostat], phase: "active",
    message: "Misstänkt extravärme. Det kan vara eldning eller sol.", active_since: now - 3600,
    contribution: -1, control_note: "Extra kaminbidrag −1 steg. Övervakning: inga automatiska ändringar skickas.",
    completed: 3, learned: true, duration_hours: 4.2,
    room_effects: [{ entity_id: "sensor.living", rise: 0.4, arrival_minutes: 45 }],
    recent: [{ start: now - 86400, end: now - 72000, complete: false }],
  };
  await firePage.evaluate(() => window.dispatchEvent(new Event("online")));
  await firePage.waitForFunction(() => document.getElementById("fireplace-status").textContent.includes("Misstänkt extravärme"));
  assert.match(await firePage.locator("#fireplace-learning").textContent(), /4,2 timmar.*3 avslutade.*Ingen säker prognos/);
  assert.match(await firePage.locator("#fireplace-effects").textContent(), /Vardagsrum.*0,4 °C.*45 min.*Inte säkert/);
  assert.match(await firePage.locator("#fireplace-history").textContent(), /avbruten\/osäker/);
  assert.match(await firePage.locator("#fireplace-control").textContent(), /kaminbidrag −1.*Övervakning/);
  assert.match(await firePage.locator("#room-impact-list").textContent(), /prioriterat.*kaminrum/);
  assert.equal(fireWrites, 1, "Rendering suspected extra heat must not save or send new commands");
  await firePage.setViewportSize({ width: 390, height: 844 });
  assert.equal(await firePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  await firePage.close();
}
try {
  if (!process.env.ENERGY_ONLY) await dashboardChecks();
  const energyPage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  energyPage.on("pageerror", (error) => errors.push(error.message));
  let energySettings = { ...defaults, room_sensors: ["sensor.living"], offset_entity: offset, price_entity: price };
  let energyWrites = 0, energyReads = 0, energyActive = 0, energyMaxActive = 0;
  const energyEnd = Math.floor(now / 3600) * 3600;
  const energyHours = Array.from({ length: 24 }, (_, i) => ({
    start: energyEnd - (24 - i) * 3600, end: energyEnd - (23 - i) * 3600,
    kwh: 1, cost: 2, coverage_seconds: 3600, priced_seconds: 3600,
  }));
  let energyFail = false, energyPartial = false, holdEnergy = false, releaseEnergy, energyResponse;
  await energyPage.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") { energySettings = route.request().postDataJSON(); energyWrites++; }
    return route.fulfill({ json: energySettings });
  });
  await energyPage.route("**/api/energy", async (route) => {
    energyReads++; energyActive++; energyMaxActive = Math.max(energyMaxActive, energyActive);
    const provider = energySettings.price_provider;
    if (holdEnergy) { holdEnergy = false; await new Promise((resolve) => { releaseEnergy = resolve; }); }
    energyActive--;
    if (energyResponse) return route.fulfill(energyResponse);
    const power = !energySettings.energy_entity || energySettings.energy_entity === "sensor.pump_el_current_power_consumption";
    return route.fulfill(energyFail ? { status: 502, json: { error: "Recorder otillgänglig" } } : { json: {
      start: energyEnd - 86400, end: energyEnd, currency: "SEK", price_provider: provider,
      method: power ? "power" : "counter",
      price_entity: energySettings.price_entity, meter: {
        entity_id: power ? "sensor.pump_el_current_power_consumption" : "sensor.pump_energy_consumed",
        name: power ? "Pumpens totala eleffekt" : "Pumpens använda el",
        automatic: !energySettings.energy_entity },
      hours: energyHours.map((hour, i) => energyPartial && i === 0 ? { ...hour, kwh: null, cost: null, coverage_seconds: 0, priced_seconds: 0 } : hour),
      totals: { kwh: energyPartial ? null : 24, cost: energyPartial ? null : 48,
        measured_kwh: energyPartial ? 23 : 24, partial_cost: energyPartial ? 46 : 48,
        coverage_seconds: (energyPartial ? 23 : 24) * 3600, priced_seconds: (energyPartial ? 23 : 24) * 3600 },
      skipped_intervals: energyPartial ? 1 : 0, price_error: null, changes_error: null,
      changes: [{ id: 1, timestamp: energyEnd - 7200, kind: "offset", status: "confirmed", value: 2, previous: 1 }],
      settings_changes: [{ timestamp: energyEnd - 3600, label: "Inställningar sparade." }],
      comparisons: [{ timestamp: energyEnd - 7200, kind: "offset", value: 2, before: energyHours[20], after: energyHours[23],
        difference_kwh: 0, difference_cost: 0 }],
    } });
  });
  await energyPage.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await energyPage.waitForFunction(() => document.getElementById("energy-status").textContent.includes("Automatiskt från myUplink"));
  assert.match(await energyPage.locator("#energy-metrics").textContent(), /Uppskattad el/);
  assert.match(await energyPage.locator("#energy-price-status").textContent(), /eleffekt.*15 minuter/);
  assert.ok(energyReads >= 1, "Blank meter selection automatically loads the myUplink energy report");
  assert.equal(energyWrites, 0, "Automatic discovery must not save settings or enable control");
  await energyPage.locator("#settings-panel > summary").click();
  assert.equal(await energyPage.locator('#energy_entity option[value="sensor.pump_energy_produced_for_heating_current_hour"]').count(), 0);
  assert.equal(await energyPage.locator('#energy_entity option[value="sensor.pump_10_years_consumption"]').count(), 0);
  assert.equal(await energyPage.locator('#energy_entity option[value="sensor.pump_el_current_power_consumption"]').count(), 1);
  await energyPage.locator("#energy_entity").selectOption("sensor.pump_energy_consumed");
  assert.match(await energyPage.locator('#energy_entity option[value=""]').textContent(), /Automatiskt.*myUplink/);
  assert.equal(energyWrites, 0, "Meter selection remains a draft until saved");
  await energyPage.locator("#save-near").click();
  await energyPage.waitForFunction(() => document.querySelector("#energy-chart svg"));
  await energyPage.waitForFunction(() => document.getElementById("energy-status").textContent.includes("Manuellt vald elmätare"));
  assert.match(await energyPage.locator("#energy-metrics").textContent(), /hela perioden.*24 kWh.*48 SEK/);
  assert.equal(await energyPage.locator("#energy-chart .chart-event").count(), 2);
  assert.match(await energyPage.locator("#energy-price-status").textContent(), /Tibber.*24 av 24/);
  await energyPage.locator("#energy-section details").first().evaluate((node) => { node.open = true; });
  assert.match(await energyPage.locator("#energy-comparisons").textContent(), /Kurvförskjutning.*Före.*Efter/);
  await energyPage.locator("#energy-section details").last().evaluate((node) => { node.open = true; });
  assert.equal(await energyPage.locator("#energy-table tbody tr").count(), 24);
  energyPartial = true;
  await energyPage.locator("#refresh-energy").click();
  await energyPage.waitForFunction(() => document.getElementById("energy-status").textContent.includes("23 av 24"));
  assert.match(await energyPage.locator("#energy-metrics").textContent(), /endast täckt del.*23 kWh.*endast prissatt del.*46 SEK/);
  assert.equal(await energyPage.locator("#energy-table tbody tr").first().locator("td").nth(1).textContent(), "—");
  await energyPage.setViewportSize({ width: 390, height: 844 });
  await energyPage.waitForFunction(() => Number(document.querySelector("#energy-chart svg").getAttribute("viewBox").split(" ")[2])
    === Math.round(document.getElementById("energy-chart").clientWidth));
  await energyPage.locator("#energy-section").screenshot({ path: `${artifacts}/energy-mobile.png` });
  assert.equal(await energyPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  energyFail = true;
  await energyPage.locator("#refresh-energy").click();
  await energyPage.waitForFunction(() => document.getElementById("energy-status").textContent.includes("Recorder otillgänglig"));
  assert.equal(await energyPage.locator("#energy-metrics").textContent(), "", "Failed reads clear old numbers");
  energyFail = false;
  energyResponse = { status: 502, contentType: "text/html", body: "<html><body>Bad Gateway</body></html>" };
  await energyPage.locator("#refresh-energy").click();
  await energyPage.waitForFunction(() => document.getElementById("energy-status").textContent.includes("HTTP 502"));
  assert.match(await energyPage.locator("#energy-status").textContent(), /ingress.*api\/energy, HTTP 502/);
  assert.equal(await energyPage.locator("#energy-data").isVisible(), false, "HTML gateway failures never leave old totals visible");
  for (const response of [
    { status: 200, contentType: "text/html", body: "<html><body>Login</body></html>" },
    { status: 200, contentType: "application/json", body: '{"hours":' },
    { status: 503, contentType: "application/json", body: "" },
    { status: 502, json: null },
  ]) {
    energyResponse = response;
    await energyPage.locator("#refresh-energy").click();
    await energyPage.waitForFunction((status) => document.getElementById("energy-status").textContent.includes(`HTTP ${status}`), response.status);
    const message = await energyPage.locator("#energy-status").textContent();
    assert.match(message, /api\/energy/);
    assert.doesNotMatch(message, /expected pattern|Unexpected token|Cannot read|\.\./);
    assert.equal(await energyPage.locator("#energy-data").isVisible(), false);
  }
  energyResponse = undefined;
  await energyPage.evaluate(() => void refreshEnergy());
  await energyPage.waitForFunction(() => !document.getElementById("energy-data").hidden);
  holdEnergy = true;
  const readsBefore = energyReads;
  await energyPage.evaluate(() => void refreshEnergy());
  for (let attempt = 0; attempt < 50 && !releaseEnergy; attempt++) await energyPage.waitForTimeout(20);
  assert.equal(typeof releaseEnergy, "function");
  await energyPage.evaluate(() => { settings.price_provider = "nordpool"; void refreshEnergy(); void refreshEnergy(); });
  energySettings.price_provider = "nordpool";
  releaseEnergy();
  await energyPage.waitForFunction(() => document.getElementById("energy-price-status").textContent.startsWith("Nord Pool"));
  assert.equal(energyReads, readsBefore + 2, "Many refreshes coalesce to one follow-up after the source change");
  assert.equal(energyMaxActive, 1, "Energy reads never overlap");
  assert.equal(energyWrites, 1, "Viewing and refreshing energy does not save settings or control the pump");
  await energyPage.close();
  const auxiliaryPage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  auxiliaryPage.on("pageerror", (error) => errors.push(error.message));
  let auxiliaryFail = false, auxiliaryMissing = false, auxiliaryStale = false, auxiliaryEmpty = false, auxiliaryNoCoverage = false;
  let auxiliaryHold = false, releaseAuxiliary, auxiliaryReads = 0, auxiliaryActive = 0, auxiliaryMaxActive = 0;
  let auxiliarySettings = { ...defaults, offset_entity: offset, price_entity: price };
  await auxiliaryPage.route("**/api/settings", (route) => route.fulfill({ json: auxiliarySettings }));
  await auxiliaryPage.route("**/api/auxiliary", async (route) => {
    auxiliaryReads++; auxiliaryActive++; auxiliaryMaxActive = Math.max(auxiliaryMaxActive, auxiliaryActive);
    const provider = auxiliarySettings.price_provider;
    if (auxiliaryHold) { auxiliaryHold = false; await new Promise((resolve) => { releaseAuxiliary = resolve; }); }
    auxiliaryActive--;
    if (auxiliaryFail) return route.fulfill({ status: 502, json: { error: "Elpatronhistoriken saknas." } });
    const priced = !auxiliaryMissing;
    return route.fulfill({ json: {
      start: energyEnd - 86400, end: energyEnd, checked_at: now,
      method: "power", meter: { entity_id: "sensor.pump_power_internal_add_heat", name: "Elpatronens egen effekt" },
      current: { active: auxiliaryStale ? null : true, kw: auxiliaryStale ? null : 3,
        error: auxiliaryStale ? "Mätvärdet är för gammalt." : null },
      hours: energyHours, currency: priced ? "SEK" : null, price_provider: provider,
      price_entity: price, price_error: priced ? null : "Prishistorik saknas.",
      skipped_intervals: 2, runtime_seconds: auxiliaryEmpty ? 0 : 600, observed_starts: auxiliaryEmpty ? 0 : 1,
      totals: { measured_kwh: auxiliaryEmpty ? 0 : 0.5, partial_cost: priced && !auxiliaryEmpty ? 1 : 0,
        coverage_seconds: auxiliaryNoCoverage ? 0 : 3600, priced_seconds: priced && !auxiliaryNoCoverage ? 3600 : 0 },
      runs: auxiliaryEmpty ? [] : [{
        start: energyEnd - 3600, end: energyEnd - 3000, seconds: 600, kwh: 0.5, max_kw: 3,
        start_observed: true, end_observed: false, cost: priced ? 1 : null, partial_cost: priced ? 1 : 0,
        priced_seconds: priced ? 600 : 0,
        context: { operation: { value: "Hot water", unit: "" }, outdoor: { value: "-4", unit: "°C" },
          degree_minutes: { value: "-350", unit: "DM" }, hot_water: null },
      }],
    } });
  });
  await auxiliaryPage.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await auxiliaryPage.waitForFunction(() => !document.getElementById("auxiliary-data").hidden);
  assert.match(await auxiliaryPage.locator("#auxiliary-current").textContent(), /i drift.*3 kW/);
  assert.match(await auxiliaryPage.locator("#auxiliary-metrics").textContent(), /körperioder.*1 st.*10 min.*0,5 kWh.*1 SEK/);
  assert.match(await auxiliaryPage.locator("#auxiliary-coverage").textContent(), /1 av 24 timmar.*1 start/);
  await auxiliaryPage.locator("#auxiliary-section details").evaluate((node) => { node.open = true; });
  assert.match(await auxiliaryPage.locator("#auxiliary-runs").textContent(), /Start observerad.*Slut okänt.*Varmvatten.*−?4.*350.*okänt/);
  assert.match(await auxiliaryPage.locator("#auxiliary-section").textContent(), /inte en säker startorsak/);
  auxiliaryStale = true; auxiliaryMissing = true;
  await auxiliaryPage.locator("#refresh-auxiliary").click();
  await auxiliaryPage.waitForFunction(() => document.getElementById("auxiliary-current").textContent.includes("okänt"));
  assert.match(await auxiliaryPage.locator("#auxiliary-price-status").textContent(), /Prishistorik saknas/);
  assert.match(await auxiliaryPage.locator("#auxiliary-runs").textContent(), /—.*prissatt del —/);
  auxiliaryFail = true;
  await auxiliaryPage.locator("#refresh-auxiliary").click();
  await auxiliaryPage.waitForFunction(() => document.getElementById("auxiliary-status").textContent.includes("Elpatronhistoriken saknas"));
  assert.equal(await auxiliaryPage.locator("#auxiliary-data").isVisible(), false);
  assert.equal(await auxiliaryPage.locator("#auxiliary-metrics").textContent(), "");
  auxiliaryFail = false; auxiliaryEmpty = true;
  await auxiliaryPage.locator("#refresh-auxiliary").click();
  await auxiliaryPage.waitForFunction(() => !document.getElementById("auxiliary-data").hidden);
  assert.match(await auxiliaryPage.locator("#auxiliary-runs").textContent(), /Inga körperioder.*säger inget om dataluckorna/);
  auxiliaryNoCoverage = true;
  await auxiliaryPage.locator("#refresh-auxiliary").click();
  await auxiliaryPage.waitForFunction(() => document.getElementById("auxiliary-runs").textContent.includes("Underlag saknas"));
  assert.doesNotMatch(await auxiliaryPage.locator("#auxiliary-metrics").textContent(), /0 st|0 min|0 kWh|0 SEK/);
  auxiliaryNoCoverage = false;
  auxiliaryHold = true;
  const auxiliaryBefore = auxiliaryReads;
  await auxiliaryPage.evaluate(() => void refreshAuxiliary());
  for (let attempt = 0; attempt < 50 && !releaseAuxiliary; attempt++) await auxiliaryPage.waitForTimeout(20);
  assert.equal(typeof releaseAuxiliary, "function");
  await auxiliaryPage.evaluate(() => { settings.price_provider = "nordpool"; void refreshAuxiliary(); void refreshAuxiliary(); });
  auxiliarySettings.price_provider = "nordpool";
  releaseAuxiliary();
  await auxiliaryPage.waitForFunction(() => document.getElementById("auxiliary-price-status").textContent.startsWith("Nord Pool"));
  assert.equal(auxiliaryReads, auxiliaryBefore + 2);
  assert.equal(auxiliaryMaxActive, 1);
  auxiliaryEmpty = false;
  await auxiliaryPage.locator("#refresh-auxiliary").click();
  await auxiliaryPage.waitForFunction(() => document.querySelector("#auxiliary-runs table"));
  await auxiliaryPage.setViewportSize({ width: 390, height: 844 });
  await auxiliaryPage.waitForFunction(() => Number(document.querySelector("#auxiliary-chart svg").getAttribute("viewBox").split(" ")[2])
    === Math.round(document.getElementById("auxiliary-chart").clientWidth));
  assert.equal(await auxiliaryPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  assert.equal(await auxiliaryPage.locator("#auxiliary-runs").evaluate((node) => node.scrollWidth > node.clientWidth), true,
    "The run table scrolls horizontally instead of squeezing headings into individual letters");
  await auxiliaryPage.locator("#auxiliary-section").screenshot({ path: `${artifacts}/auxiliary-mobile.png` });
  await auxiliaryPage.close();
  assert.deepEqual(errors, []);
  console.log(process.env.ENERGY_ONLY ? "Energy and auxiliary UI passed: selection, runs, context, costs, errors, recovery and mobile layout."
    : "Dashboard UI passed: climate picker, active settings preserved, Tibber discovery, real SVG graphs, forecast, alarms, electricity labels, audit events, errors and mobile layout.");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
