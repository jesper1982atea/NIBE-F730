import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { chromium } from "playwright";

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
let failHistory = false;
const now = Date.now() / 1000;
const entities = [
  { entity_id: "sensor.living", name: "Vardagsrum", state: "22", unit: "°C", device_class: "temperature" },
  { entity_id: thermostat, name: "LK Allrum", state: "heat", current_temperature: 22.2, temperature_error: null },
  { entity_id: offset, name: "NIBE Offset", state: "3", min: -10, max: 10, step: 1 },
  { entity_id: outdoor, name: "NIBE BT1", state: "11", unit: "°C", device_class: "temperature" },
  { entity_id: price, name: "Tibber elpris", state: "1.4", unit: "SEK/kWh" },
  { entity_id: "climate.lk_badrum_overvaningen_thermostat_with_a_very_long_identifier", name: "LK Badrum övervåningen Thermostat med extra långt namn", state: "heat", current_temperature: 25.7, temperature_error: null },
  { entity_id: "sensor.nibe_f730_heating_offset_smart_price_adaption_extra_long", name: "NIBE F730 Heating offset smart price adaption", state: "0", unit: "°C", device_class: "temperature" },
  { entity_id: "weather.smhi_home", name: "SMHI hemma", state: "cloudy" },
];
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
    indoor: 22, current_offset: 3, desired_offset: 1.5, next_offset: 2,
    enabled: settings.enabled, checked_at: now, wait_seconds: 1800,
    message: "Väntar på nästa tillåtna ändring.", contributions: { room: -1.5, weather: 0, price: 0 },
  };
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
      preheat: { enabled: settings.preheat_enabled, degrees: settings.preheat_degrees, lead_hours: 3, lead_source: "default", note: null,
        windows: [{ kind: "preheat", start: now + 7 * hour, end: now + 10 * hour, shift: 0.5, price: 0.9 },
                  { kind: "coast", start: now + 10 * hour, end: now + 13 * hour, shift: -0.5, price: 2.4 }] },
    };
  }
  else if (path === "/api/events") data = events;
  else if (path === "/api/history") {
    if (failHistory) { response.writeHead(502); response.end('{"error":"Recorder saknas"}'); return; }
    data = history();
  } else if (path === "/api/forecast") data = {
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
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}${prefix}/`);
  await page.waitForFunction(() => document.getElementById("forecast-chart").querySelector("svg"));
  await page.waitForFunction(() => document.querySelectorAll("#events-list article").length === 4);
  await page.waitForFunction(() => document.getElementById("plan-curve").querySelector("svg"));
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
  assert.match(await page.locator("#outdoor-now").textContent(), /^\d/);
  assert.match(await page.locator("#outdoor-forecast").textContent(), /Närmaste dygnet .* °C · kallast/);
  assert.match(await page.locator("#next-change").textContent(), /^(kl|i morgon) \d\d:\d\d$/);
  assert.match(await page.locator("#next-change-why").textContent(), /Höjer värmen ett steg \(2→3\) · billigt elpris/);
  assert.match(await page.locator("#hero-extra").textContent(), /Elpris nu .* SEK\/kWh \((billigt|normalt|dyrt)\)/);
  await page.waitForFunction(() => document.getElementById("hw-status").textContent.startsWith("Uppdaterad"));
  await page.waitForFunction(() => document.getElementById("learn-status").textContent.startsWith("Grov bild"));
  assert.equal(await page.locator("#learning-card").evaluate((card) => card.previousElementSibling.querySelector("#plan-changes") !== null), true, "Learning card follows the plan");
  assert.equal(await page.locator("#hot-water-card").evaluate((card) => card.previousElementSibling.id), "learning-card");
  assert.match(await page.locator("#learn-progress").textContent(), /6,6 av 7 dygn.*4 av 6/);
  assert.match(await page.locator("#learn-facts").textContent(), /\+0,19 °C inne på sikt.*≈ 7,1 timmar/);
  assert.equal(await page.locator("#learn-chart path").count(), 2);
  assert.equal(await page.locator("#learn-chart .chart-band.preheat").count(), 1);
  assert.equal(await page.locator("#learn-chart .chart-band.coast").count(), 1);
  assert.match(await page.locator("#learn-windows").textContent(), /Förvärmning är av.*3 timmar före \(standard.*förvärm \+0,5°.*0,5° svalare/);
  assert.equal(await page.locator("#preheat_enabled").isChecked(), false, "Pre-heating starts off");
  assert.equal(await page.locator("#preheat_degrees").inputValue(), "0.5");
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
  assert.equal(await page.locator("#save-target").isVisible(), false);
  await page.locator("#target-up").click();
  assert.equal(await page.locator("#save-target").isVisible(), true);
  await page.locator("#target-down").click();
  assert.equal(await page.locator("#save-target").isVisible(), false);
  assert.equal(await page.locator("#enabled").isChecked(), true);
  assert.equal(await page.locator("#price_entity").inputValue(), price);
  assert.equal(await page.locator("#manual-price").getAttribute("open"), null);
  assert.equal(writes, 0, "Discovery and dashboard reads must not save or activate anything");
  assert.equal(await page.locator("#temperature-chart path").count(), 1);
  assert.equal(await page.locator("#forecast-chart path").count(), 1);
  assert.match(await page.locator("#pump-metrics").textContent(), /Larm aktivt/);
  assert.equal(await page.locator("#pump-metrics .alarm-active").count(), 1);
  assert.match(await page.locator("#electricity-metrics").textContent(), /Pumpens elförbrukning.*Okänt/);
  assert.match(await page.locator("#electricity-note").textContent(), /hela huset/);
  assert.match(await page.locator("#events-list").textContent(), /Förskjutning 3 → 2/);
  assert.match(await page.locator("#events-list").textContent(), /Automatik: av → på/);
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
    const cards = page.locator("main > section.card");
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
    await page.locator("main > section.card").nth(1).screenshot({ path: `${shots}/morkt-varmeplan.png` });
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
  await panel.locator("#plan-curve svg").waitFor();
  const frame = haPage.frame({ url: /dashboard-test/ });
  assert.equal(await frame.evaluate(() => document.documentElement.dataset.theme), "dark", "Must follow dark Home Assistant theme");
  await haPage.evaluate(() => { document.documentElement.style.setProperty("--primary-background-color", "#fafafa"); document.body.style.background = "#fafafa"; });
  await frame.waitForFunction(() => document.documentElement.dataset.theme === "light");
  await haPage.close();
  failHistory = true;
  await page.locator("#refresh-history").click();
  await page.waitForFunction(() => document.getElementById("history-status").textContent.includes("Recorder saknas"));
  assert.equal(await page.locator("#temperature-chart svg").count(), 0);
  assert.equal(await page.locator("#enabled").isChecked(), true, "History failure must not change automation");
  assert.deepEqual(errors, []);
  console.log("Dashboard UI passed: climate picker, active settings preserved, Tibber discovery, real SVG graphs, forecast, alarms, electricity labels, audit events, errors and mobile layout.");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
