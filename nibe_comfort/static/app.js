"use strict";

const $ = (id) => document.getElementById(id);
const advanced = [
  ["base_offset", "Basförskjutning · steg", -10, 10, 0.1, "Utgångsläget som all kompensation läggs till på."],
  ["min_offset", "Lägsta förskjutning", -10, 10, 0.1, "Absolut nedre säkerhetsgräns."],
  ["max_offset", "Högsta förskjutning", -10, 10, 0.1, "Absolut övre säkerhetsgräns."],
  ["room_gain", "Rumspåverkan · steg/°C", 0, 5, 0.1, "Hur mycket en grads avvikelse påverkar förskjutningen."],
  ["deadband", "Tolerans runt målet · °C", 0, 2, 0.1, "Inom detta band ger rummen ingen extra kompensation."],
  ["max_step", "Maximal ändring per gång", 0.1, 2, 0.1, "Måste vara minst entitetens steglängd."],
  ["min_interval_minutes", "Minst mellan ändringar · minuter", 10, 1440, 1, "Väntetid gäller även efter omstart och misslyckade skrivförsök."],
  ["max_age_minutes", "Högsta ålder på mätvärden · minuter", 5, 1440, 1, "För gamla eller saknade värden pausar automatiken."],
];
let settings;
let entities = [];
let busy = false;
let discovery = null;
let discoveryError = "";
let dashboardBusy = false;
let eventRows = [];
const SVG = "http://www.w3.org/2000/svg";

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = false;
}

async function api(path, options = {}) {
  const response = await fetch(`./api/${path}`, { ...options, signal: AbortSignal.timeout(20000) });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function makeAdvanced() {
  for (const [id, title, min, max, step, hint] of advanced) {
    if ($(id)) continue;
    const wrapper = document.createElement("div");
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = title;
    const input = document.createElement("input");
    Object.assign(input, { id, name: id, type: "number", min, max, step, required: true });
    const description = document.createElement("p");
    description.className = "hint";
    description.textContent = hint;
    wrapper.append(label, input, description);
    $("advanced-fields").append(wrapper);
  }
}

function selectOptions(id, predicate) {
  const select = $(id);
  select.replaceChildren(new Option(id === "forecast_entity" ? "Automatiskt · SMHI i första hand" : "Inte vald", ""));
  for (const entity of entities.filter(predicate)) {
    select.add(new Option(`${entity.name} · ${entity.entity_id}`, entity.entity_id));
  }
  const configured = settings[id];
  if (configured && !Array.from(select.options).some((option) => option.value === configured)) {
    select.add(new Option(`${configured} (saknas / ej tillgänglig)`, configured));
  }
  select.value = configured;
}

function entityHints() {
  const offset = entities.find((entity) => entity.entity_id === $("offset_entity").value);
  $("pump-connection").textContent = offset
    ? `Ansluten: ${offset.name} · ${offset.entity_id}`
    : "Ingen pumpanslutning vald.";
  const outdoor = entities.find((entity) => entity.entity_id === $("outdoor_entity").value);
  $("outdoor-connection").textContent = outdoor
    ? `Utegivare: ${outdoor.name} · ${outdoor.state} ${outdoor.unit}`
    : "Ingen utegivare vald.";
  $("offset-details").textContent = offset
    ? `Rapporterat värde: ${offset.state}. Intervall: ${offset.min} till ${offset.max}. Steglängd: ${offset.step}. Enhet: ${offset.unit || "saknas"}.`
    : "Utan vald entitet kan du se temperaturförslag, men inte styra pumpen.";
  const price = entities.find((entity) => entity.entity_id === $("price_entity").value);
  $("price-unit").textContent = price
    ? `Givarens värde: ${price.state} ${price.unit}. Ange båda prisgränserna i ${price.unit || "samma enhet som givaren"}.`
    : "Välj givare för att se prisets enhet. Exempel: 1 kr/kWh = 100 öre/kWh.";
}

function renderSettings() {
  for (const [key, value] of Object.entries(settings)) {
    const input = $(key);
    if (!input || key === "room_sensors") continue;
    if (typeof value === "boolean") input.checked = value;
    else if (input.tagName !== "SELECT") input.value = value;
  }
  const isTemperature = (entity) => entity.entity_id.startsWith("sensor.")
    && (entity.device_class === "temperature" || ["°C", "°F"].includes(entity.unit));
  selectOptions("offset_entity", (entity) => entity.entity_id.startsWith("number."));
  selectOptions("outdoor_entity", (entity) => isTemperature(entity) || entity.entity_id.startsWith("weather."));
  selectOptions("price_entity", (entity) => entity.entity_id.startsWith("sensor."));
  selectOptions("forecast_entity", (entity) => entity.entity_id.startsWith("weather."));
  const rooms = entities.filter((entity) => isTemperature(entity) || entity.entity_id.startsWith("climate."));
  for (const id of settings.room_sensors) {
    if (!rooms.some((entity) => entity.entity_id === id)) rooms.push({ entity_id: id, name: id, state: "saknas", unit: "" });
  }
  $("room_sensors").replaceChildren();
  for (const entity of rooms) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    Object.assign(input, { type: "checkbox", value: entity.entity_id, checked: settings.room_sensors.includes(entity.entity_id) });
    const text = document.createElement("span");
    const climate = entity.entity_id.startsWith("climate.");
    text.textContent = climate
      ? `${entity.name} · ${displayNumber(entity.current_temperature)} °C · golvvärme/termostat${entity.temperature_error ? " (saknar mätvärde)" : ""}`
      : `${entity.name} · ${entity.state} ${entity.unit}`;
    const id = document.createElement("small");
    id.textContent = entity.entity_id;
    text.append(id);
    label.append(input, text);
    $("room_sensors").append(label);
  }
  if (!rooms.length) $("room_sensors").textContent = "Inga temperaturgivare hittades i Home Assistant.";
  $("mode").textContent = settings.enabled ? "Automatik aktiverad" : "Övervakning";
  $("mode").classList.toggle("active", settings.enabled);
  $("discovery-message").textContent = discoveryError || discovery?.message || "Automatisk upptäckt är inte tillgänglig.";
  $("manual-pump").open = !settings.offset_entity;
  $("manual-outdoor").open = !settings.outdoor_entity;
  $("manual-price").open = !settings.price_entity;
  $("price-discovery").textContent = discovery?.price_message || "Välj prisgivare manuellt om den inte hittas.";
  entityHints();
}

async function findPump() {
  try {
    discovery = await api("discovery");
    // Only fill missing fields. Never switch an existing pump or overwrite user choices.
    if (!settings.offset_entity && discovery.offset_entity) {
      settings.offset_entity = discovery.offset_entity;
      const offset = entities.find((entity) => entity.entity_id === discovery.offset_entity);
      const current = Number(offset?.state);
      if (offset && offset.state !== "" && Number.isFinite(current)
        && current >= settings.min_offset && current <= settings.max_offset) {
        settings.base_offset = current;
      }
    }
    if (!settings.outdoor_entity && settings.offset_entity === discovery.offset_entity) {
      settings.outdoor_entity = discovery.outdoor_entity;
    }
    if (!settings.price_entity && discovery.price_entity) settings.price_entity = discovery.price_entity;
  } catch (error) {
    discoveryError = `Kunde inte upptäcka NIBE automatiskt: ${error.message}. Du kan välja anslutningen manuellt.`;
  }
}

function readSettings() {
  const result = { ...settings };
  for (const [key, value] of Object.entries(settings)) {
    if (key === "room_sensors") {
      result[key] = Array.from($("room_sensors").querySelectorAll("input:checked"), (input) => input.value);
    } else if (typeof value === "boolean") result[key] = $(key).checked;
    else if (typeof value === "number") result[key] = $(key).valueAsNumber;
    else result[key] = $(key).value;
  }
  return result;
}

function displayNumber(value) {
  return typeof value === "number" ? value.toLocaleString("sv-SE", { maximumFractionDigits: 2 }) : "—";
}

async function refreshStatus() {
  try {
    const state = await api("status");
    if (typeof state.enabled === "boolean") {
      $("mode").textContent = state.enabled ? "Automatik aktiverad" : "Övervakning";
      $("mode").classList.toggle("active", state.enabled);
    }
    $("indoor").textContent = displayNumber(state.indoor);
    $("current-offset").textContent = displayNumber(state.current_offset);
    $("desired-offset").textContent = displayNumber(state.desired_offset);
    $("next-offset").textContent = displayNumber(state.next_offset);
    $("status-title").textContent = state.error_message ? "Styrningen är pausad" : state.enabled ? "Värmen följer dina inställningar" : "Se förslaget innan du styr";
    $("status-message").textContent = state.error_message || state.message;
    const checked = state.checked_at ? new Date(state.checked_at * 1000).toLocaleTimeString("sv-SE") : "väntar";
    $("updated").textContent = `Senast avläst: ${checked}${state.wait_seconds ? ` · Nästa ändring tidigast om ${Math.ceil(state.wait_seconds / 60)} min` : ""}`;
    if (state.contributions) {
      const c = state.contributions;
      $("explanation").textContent = `Bas + rum (${displayNumber(c.room)}) + väder (${displayNumber(c.weather)}) + elpris (${displayNumber(c.price)}) → begränsat mål ${displayNumber(state.desired_offset)}. Nästa steg anpassas till pumpens steglängd och dina gränser.`;
    } else {
      $("explanation").textContent = "Inget aktuellt förslag. Kontrollera inställningarna och statusmeddelandet.";
    }
  } catch (error) {
    $("status-title").textContent = "Kontakt med tillägget saknas";
    $("status-message").textContent = error.message;
    for (const id of ["indoor", "current-offset", "desired-offset", "next-offset"]) $(id).textContent = "—";
    $("updated").textContent = "Visningen är inte aktuell.";
  }
}

async function save(config) {
  if (busy) return;
  busy = true;
  $("save").disabled = $("pause").disabled = true;
  try {
    settings = await api("settings", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config),
    });
    renderSettings();
    notice("Inställningarna är sparade. Status och förslag uppdateras vid nästa avläsning, inom en minut.");
    await refreshStatus();
    void refreshDashboard();
    void refreshHistory();
    void refreshForecast();
    void refreshPlan();
  } catch (error) {
    notice(`Kunde inte spara: ${error.message}`, true);
  } finally {
    busy = false;
    $("save").disabled = $("pause").disabled = false;
  }
}

function svgElement(name, attributes = {}, text) {
  const element = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}

const timeLabel = (time, span) => new Date(time * 1000).toLocaleString("sv-SE", span > 30 * 3600
  ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" });

// Sensors only report changes, so a value is drawn as held until the next report (step line).
function linePath(line, x, y, end) {
  let path = "", open = false;
  line.points.forEach(([stamp, value], index) => {
    if (value === null) { open = false; return; }
    const next = line.points[index + 1];
    if (line.step === false) {
      path += `${open ? "L" : "M"}${x(stamp).toFixed(2)},${y(value).toFixed(2)} `;
    } else {
      path += open ? `H${x(stamp).toFixed(2)} V${y(value).toFixed(2)} ` : `M${x(stamp).toFixed(2)},${y(value).toFixed(2)} `;
      if (!next || next[1] === null) path += `H${x(next ? next[0] : line.until ?? end).toFixed(2)} `;
    }
    open = true;
  });
  return path;
}

function chartWidth(container) {
  return Math.max(280, Math.min(1100, Math.round(container.clientWidth || 760)));
}

const signed = (from, to) => `${to > from ? "▲" : "▼"} ${displayNumber(from)}→${displayNumber(to)}`;

function drawChart(container, series, options) {
  const { start, end, unit = "°", now = null, guides = [], markers = [], label } = options;
  container.replaceChildren();
  const values = series.flatMap((line) => line.points.filter((point) => point[1] !== null).map((point) => point[1]));
  if (!values.length) {
    container.textContent = options.empty || "Inga mätpunkter att visa.";
    return;
  }
  values.push(...guides.map((guide) => guide.value));
  const pad = options.pad ?? 0.5;
  let minimum = Math.min(...values) - pad, maximum = Math.max(...values) + pad;
  if (options.integer !== false) { minimum = Math.floor(minimum); maximum = Math.ceil(maximum); }
  if (maximum - minimum < 1e-9) { minimum -= 1; maximum += 1; }
  const width = chartWidth(container), narrow = width < 520;
  const height = options.height ?? (narrow ? 220 : 280);
  const left = narrow ? 46 : 56, right = narrow ? 8 : 20, top = markers.length ? 40 : 16, bottom = 30;
  const ticks = narrow ? 2 : 4;
  const x = (time) => left + (Math.max(start, Math.min(end, time)) - start) / (end - start) * (width - left - right);
  const y = (value) => top + (maximum - value) / (maximum - minimum) * (height - top - bottom);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  for (let index = 0; index <= 4; index++) {
    const value = minimum + (maximum - minimum) * index / 4;
    svg.append(svgElement("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "chart-grid" }));
    svg.append(svgElement("text", { x: left - 6, y: y(value) + 4, "text-anchor": "end" }, `${displayNumber(value)}${unit}`));
  }
  for (let index = 0; index <= ticks; index++) {
    const time = start + (end - start) * index / ticks;
    svg.append(svgElement("text", { x: x(time), y: height - 9, "text-anchor": index === 0 ? "start" : index === ticks ? "end" : "middle" },
      timeLabel(time, end - start)));
  }
  for (const guide of guides) {
    svg.append(svgElement("line", { x1: left, x2: width - right, y1: y(guide.value), y2: y(guide.value), class: `chart-target ${guide.className || ""}` }));
    if (guide.label) svg.append(svgElement("text", { x: width - right - 4, y: y(guide.value) - 5, "text-anchor": "end", class: "chart-guide-label" }, guide.label));
  }
  if (now !== null && now > start && now < end) {
    svg.append(svgElement("line", { x1: x(now), x2: x(now), y1: top, y2: height - bottom, class: "chart-now" }));
    svg.append(svgElement("text", { x: x(now) + 4, y: height - bottom - 6, class: "chart-guide-label" }, "nu"));
  }
  // Vertical event markers with labels in two staggered rows so neighbouring labels do not collide.
  const rows = [-Infinity, -Infinity];
  for (const marker of [...markers].sort((a, b) => a.time - b.time)) {
    if (marker.time < start || marker.time > end) continue;
    const px = x(marker.time);
    const text = narrow ? marker.short : marker.label;
    const textWidth = text.length * 6.6 + 10;
    const anchorLeft = px + textWidth > width - right;
    const from = anchorLeft ? px - textWidth : px;
    const row = rows[0] <= from ? 0 : rows[1] <= from ? 1 : (rows[0] <= rows[1] ? 0 : 1);
    rows[row] = from + textWidth + 4;
    const group = svgElement("g", { class: `chart-event ${marker.className}` });
    group.append(svgElement("line", { x1: px, x2: px, y1: 6 + row * 16, y2: height - bottom }));
    group.append(svgElement("rect", { x: from, y: 6 + row * 16 - 1, width: textWidth, height: 15, rx: 4 }));
    group.append(svgElement("text", { x: from + 5, y: 6 + row * 16 + 10 }, text));
    group.append(svgElement("title", {}, marker.title || marker.label));
    svg.append(group);
  }
  series.forEach((line, index) => {
    const color = `series-${(line.color ?? index) % 10}`;
    const curve = svgElement("path", { d: linePath(line, x, y, end), class: `chart-line ${color}${line.dashed ? " dashed" : ""}${line.thin ? " thin" : ""}` });
    curve.append(svgElement("title", {}, line.name));
    svg.append(curve);
    for (const [stamp, value] of line.markers || []) {
      svg.append(svgElement("circle", { cx: x(stamp), cy: y(value), r: 4.5, class: `chart-marker ${color}` }));
    }
  });
  if (label) container.setAttribute("aria-label", label);
  container.append(svg);
}

function legend(container, series) {
  container.replaceChildren(...series.map((line, index) => {
    const text = document.createElement("span");
    text.className = `legend-item series-${(line.color ?? index) % 10}${line.dashed ? " dashed" : ""}`;
    text.textContent = line.name;
    return text;
  }));
}

function drawCurve(container, curve) {
  container.replaceChildren();
  const width = chartWidth(container), narrow = width < 520;
  const height = narrow ? 240 : 300, left = narrow ? 44 : 56, right = narrow ? 8 : 20, top = 16, bottom = 40;
  const others = curve.points.filter((point) => !curve.fit || Math.abs(point[2] - curve.fit.offset) > 1e-6);
  const own = curve.points.filter((point) => curve.fit && Math.abs(point[2] - curve.fit.offset) <= 1e-6);
  const xs = curve.points.map((point) => point[0]), ys = curve.points.map((point) => point[1]);
  if (curve.forecast_range) xs.push(...curve.forecast_range);
  if (!curve.points.length) { container.textContent = curve.error || "Ingen kurva att visa."; return; }
  const fitAt = (outdoor) => curve.fit.intercept + curve.fit.slope * outdoor;
  const xMin = Math.floor(Math.min(...xs) - 1), xMax = Math.ceil(Math.max(...xs) + 1);
  if (curve.fit) ys.push(fitAt(xMin), fitAt(xMax));
  const yMin = Math.floor(Math.min(...ys) - 1), yMax = Math.ceil(Math.max(...ys) + 1);
  const x = (value) => left + (value - xMin) / (xMax - xMin) * (width - left - right);
  const y = (value) => top + (yMax - value) / (yMax - yMin) * (height - top - bottom);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  if (curve.forecast_range) {
    const [low, high] = curve.forecast_range;
    svg.append(svgElement("rect", { x: x(low), y: top, width: Math.max(2, x(high) - x(low)), height: height - top - bottom, class: "chart-band" }));
    svg.append(svgElement("text", { x: x(low) + 4, y: top + 12, class: "chart-guide-label" }, "prognos"));
  }
  for (let index = 0; index <= 4; index++) {
    const yValue = yMin + (yMax - yMin) * index / 4, xValue = xMin + (xMax - xMin) * index / 4;
    svg.append(svgElement("line", { x1: left, x2: width - right, y1: y(yValue), y2: y(yValue), class: "chart-grid" }));
    svg.append(svgElement("text", { x: left - 8, y: y(yValue) + 4, "text-anchor": "end" }, `${Math.round(yValue)}°`));
    svg.append(svgElement("text", { x: x(xValue), y: height - 18, "text-anchor": index === 0 ? "start" : index === 4 ? "end" : "middle" }, `${displayNumber(xValue)}°`));
  }
  svg.append(svgElement("text", { x: width - right, y: height - 2, "text-anchor": "end", class: "chart-guide-label" }, "Utetemperatur →"));
  for (const point of others) svg.append(svgElement("circle", { cx: x(point[0]), cy: y(point[1]), r: 2.5, class: "curve-point other" }));
  for (const point of own) svg.append(svgElement("circle", { cx: x(point[0]), cy: y(point[1]), r: 3, class: "curve-point" }));
  if (curve.fit) {
    svg.append(svgElement("line", { x1: x(xMin), x2: x(xMax), y1: y(fitAt(xMin)), y2: y(fitAt(xMax)), class: "chart-line series-0" }));
  }
  if (curve.current) svg.append(svgElement("circle", { cx: x(curve.current.outdoor), cy: y(curve.current.supply), r: 7, class: "curve-current" }));
  container.append(svg);
}

function metric(item) {
  const block = document.createElement("div");
  block.className = "live-metric";
  if (item.active === true) block.classList.add("alarm-active");
  const label = document.createElement("span");
  label.textContent = item.label;
  const value = document.createElement("strong");
  value.textContent = item.value === null ? "Okänt" : `${item.value} ${item.unit || ""}`.trim();
  const detail = document.createElement("small");
  detail.textContent = item.error || (item.reported_at ? `Rapporterat ${new Date(item.reported_at).toLocaleTimeString("sv-SE")}` : "");
  if (item.entity_id) block.title = item.entity_id;
  block.append(label, value, detail);
  return block;
}

async function refreshOverview() {
  try {
    const data = await api("overview");
    $("pump-metrics").replaceChildren(...data.pump.map(metric));
    $("electricity-metrics").replaceChildren(...data.pump_electricity.map(metric), ...data.house.map(metric));
    if (data.price) $("electricity-metrics").append(metric(data.price));
    if (!data.pump_electricity.length) $("electricity-metrics").prepend(metric({
      label: "Pumpens elförbrukning", value: null, error: "Ingen separat förbrukningsmätare hittades.",
    }));
    $("electricity-note").textContent = data.electricity_note;
    $("overview-status").textContent = `Senast hämtat ${new Date(data.checked_at * 1000).toLocaleTimeString("sv-SE")}.`;
  } catch (error) {
    $("pump-metrics").replaceChildren();
    $("electricity-metrics").replaceChildren();
    $("overview-status").textContent = `Kunde inte läsa pumpstatus: ${error.message}`;
  }
}

let historyData = null;

function renderHistory() {
  const data = historyData;
  const lines = data.series.filter((line) => !line.outdoor);
  const select = $("history-entity");
  const chosen = select.value;
  select.replaceChildren(new Option("Alla rum", ""), ...lines.map((line) => new Option(line.name, line.entity_id)));
  select.value = lines.some((line) => line.entity_id === chosen) ? chosen : "";
  const single = lines.find((line) => line.entity_id === select.value);
  let series, guides = [{ value: data.target, label: `ditt mål ${displayNumber(data.target)}°` }];
  if (single) {
    const color = lines.indexOf(single);
    series = [{ name: `${single.name} · uppmätt`, points: single.points, until: data.end, color }];
    if (single.kind === "climate") {
      series.push({ name: `${single.name} · inställd att värma till`, points: single.setpoints || [], until: data.end, color, dashed: true });
      guides = [];
    }
  } else {
    series = lines.map((line, index) => ({ name: line.name, points: line.points, until: data.end, color: index }));
  }
  drawChart($("temperature-chart"), series, { start: data.start, end: data.end, guides });
  legend($("temperature-legend"), series);
  const last = (points) => [...(points || [])].reverse().find((point) => point[1] !== null)?.[1] ?? null;
  const count = lines.reduce((sum, line) => sum + line.points.filter((point) => point[1] !== null).length, 0);
  let status = count
    ? `${lines.length} rumsgivare · senaste 24 timmarna · uppdaterad ${new Date(data.end * 1000).toLocaleTimeString("sv-SE")}`
    : "Ingen temperaturhistorik hittades. Kontrollera att valda givare sparas av Home Assistants Recorder.";
  if (single) {
    status = `${single.name}: uppmätt ${displayNumber(last(single.points))} °C`;
    if (single.kind === "climate") {
      const setpoint = last(single.setpoints);
      status += setpoint === null ? " · termostaten är avstängd eller saknar börvärde" : ` · inställd att värma till ${displayNumber(setpoint)} °C`;
    }
  }
  $("history-status").textContent = status;
  const table = document.createElement("table");
  const header = table.createTHead().insertRow();
  for (const name of ["Givare", "Tid", "Uppmätt", "Inställd"]) {
    const th = document.createElement("th"); th.textContent = name; header.append(th);
  }
  const body = table.createTBody();
  for (const line of single ? [single] : lines) {
    const rows = [...line.points.map(([stamp, value]) => [stamp, value, undefined]),
      ...(line.setpoints || []).map(([stamp, value]) => [stamp, undefined, value])].sort((left, right) => left[0] - right[0]);
    for (const [stamp, value, setpoint] of rows) {
      const row = body.insertRow();
      for (const text of [line.name, new Date(stamp * 1000).toLocaleString("sv-SE"),
        value === undefined ? "" : `${displayNumber(value)} °C`,
        setpoint === undefined ? "" : setpoint === null ? "av" : `${displayNumber(setpoint)} °C`]) row.insertCell().textContent = text;
    }
  }
  $("history-table").replaceChildren(table);
}

async function refreshHistory() {
  $("refresh-history").disabled = true;
  try {
    historyData = await api("history");
    renderHistory();
  } catch (error) {
    historyData = null;
    $("temperature-chart").replaceChildren();
    $("temperature-legend").replaceChildren();
    $("history-table").replaceChildren();
    $("history-status").textContent = `Kunde inte hämta historik: ${error.message}`;
  } finally { $("refresh-history").disabled = false; }
}

async function refreshForecast() {
  try {
    const data = await api("forecast");
    const points = data.points.slice(0, data.type === "hourly" ? 25 : 7);
    const start = points[0].timestamp;
    const end = Math.max(start + 3600, points.at(-1).timestamp);
    drawChart($("forecast-chart"), [{ name: "Prognos", points: points.map((point) => [point.timestamp, point.temperature]), step: false }], { start, end });
    $("forecast-status").textContent = `${data.name} · ${data.type === "hourly" ? "timprognos" : "dygnsprognos"} · hämtad ${new Date(data.fetched_at * 1000).toLocaleTimeString("sv-SE")}`;
    $("forecast-note").textContent = data.note;
    $("forecast-points").replaceChildren(...points.filter((_, index) => data.type !== "hourly" || index % 4 === 0).map((point) => {
      const text = document.createElement("span");
      text.textContent = `${new Date(point.timestamp * 1000).toLocaleString("sv-SE", { weekday: "short", hour: "2-digit", minute: "2-digit" })}: ${displayNumber(point.temperature)} °C`;
      return text;
    }));
  } catch (error) {
    $("forecast-chart").replaceChildren();
    $("forecast-points").replaceChildren();
    $("forecast-note").textContent = "";
    $("forecast-status").textContent = `Prognos saknas: ${error.message}`;
  }
}

async function refreshPlan() {
  $("refresh-plan").disabled = true;
  try {
    const data = await api("plan");
    const range = { start: data.start, end: data.end, now: data.now };
    const outdoor = [
      { name: "Utetemperatur · uppmätt", points: data.outdoor.history, until: data.now, color: 2 },
      { name: "Utetemperatur · prognos", points: data.outdoor.forecast, step: false, dashed: true, color: 2 },
    ];
    drawChart($("plan-outdoor"), outdoor, { ...range, height: 200, empty: data.outdoor.error || undefined });
    legend($("plan-outdoor-legend"), outdoor);
    $("plan-outdoor-status").textContent = data.outdoor.error || "";

    const prices = [{ name: `Elpris${data.prices.unit ? ` · ${data.prices.unit}` : ""}`, points: data.prices.points, until: data.end, color: 1 }];
    drawChart($("plan-price"), prices, {
      ...range, height: 200, unit: "", integer: false, pad: 0.1, empty: data.prices.error || undefined,
      guides: [{ value: data.prices.low, label: "billigt under" }, { value: data.prices.high, label: "dyrt över" }],
    });
    legend($("plan-price-legend"), prices);
    $("plan-price-status").textContent = data.prices.error || "";

    const done = data.offset.done || [], upcoming = data.offset.changes || [];
    const offset = [
      { name: "Kurvförskjutning · faktisk", points: data.offset.history, until: data.now, color: 0,
        markers: done.map((change) => [change.timestamp, change.offset]) },
      { name: "Beräknat mål", points: data.offset.desired || [], until: data.end, dashed: true, thin: true, color: 5 },
      { name: "Kurvförskjutning · plan", points: data.offset.planned, until: data.end, dashed: true, color: 3,
        markers: upcoming.map((change) => [change.timestamp, change.offset]) },
    ];
    const clock = (stamp) => new Date(stamp * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
    const markers = [
      ...done.map((change) => ({
        time: change.timestamp, className: change.source === "automatik" ? "done-auto" : "done-other",
        label: `${clock(change.timestamp)} ${signed(change.previous, change.offset)}${change.source === "automatik" ? "" : " (annan)"}`,
        short: signed(change.previous, change.offset),
        title: `Ändrad ${timeLabel(change.timestamp, 86400 * 2)} av ${change.source}`,
      })),
      ...upcoming.map((change) => ({
        time: change.timestamp, className: "planned",
        label: `${Math.abs(change.timestamp - data.now) < 120 ? "nu" : clock(change.timestamp)} ${signed(change.previous, change.offset)}`,
        short: signed(change.previous, change.offset),
        title: `Planerad justering: ${change.reasons.join(", ")}`,
      })),
    ];
    drawChart($("plan-offset"), offset, {
      ...range, unit: "", empty: data.offset.error || undefined, markers,
      guides: [{ value: data.offset.min, label: "lägsta" }, { value: data.offset.max, label: "högsta" }],
    });
    legend($("plan-offset-legend"), offset);
    const entry = (className, title, text) => {
      const item = document.createElement("li");
      item.className = className;
      const strong = document.createElement("strong");
      strong.textContent = title;
      item.append(strong, ` ${text}`);
      return item;
    };
    const heading = (text) => Object.assign(document.createElement("li"), { className: "plan-heading", textContent: text });
    const items = [heading("Kommande justeringar")];
    if (upcoming.length) {
      items.push(...upcoming.map((change) => entry("planned",
        `${Math.abs(change.timestamp - data.now) < 120 ? "Nu" : timeLabel(change.timestamp, 86400 * 2)}: ${signed(change.previous, change.offset)}`,
        change.reasons.length ? `· ${change.reasons.join(", ")}` : "")));
    } else {
      items.push(entry("muted", "Ingen justering planerad", "– förskjutningen ligger kvar under perioden."));
    }
    items.push(heading("Gjorda ändringar senaste dygnet"));
    const recent = done.filter((change) => change.timestamp >= data.now - 86400).reverse();
    if (recent.length) {
      items.push(...recent.map((change) => entry(change.source === "automatik" ? "done-auto" : "done-other",
        `${timeLabel(change.timestamp, 86400 * 2)}: ${signed(change.previous, change.offset)}`,
        `· ${change.source}${change.reasons.length ? ` (${change.reasons.join(", ")})` : ""}`)));
    } else {
      items.push(entry("muted", "Inga ändringar", "av kurvförskjutningen senaste dygnet."));
    }
    $("plan-changes").replaceChildren(...items);
    $("plan-notes").textContent = data.notes.join(" ");

    drawCurve($("plan-curve"), data.curve);
    const fit = data.curve.fit;
    const outside = fit && (-10 < fit.min_outdoor || 0 > fit.max_outdoor) ? " Värden utanför uppmätt utetemperatur är förlängda och osäkra." : "";
    $("plan-curve-status").textContent = data.curve.error || (fit
      ? `Vid förskjutning ${displayNumber(fit.offset)} ger pumpen ungefär ${displayNumber(fit.intercept + fit.slope * 0)} °C framledning vid 0 °C ute och ${displayNumber(fit.intercept + fit.slope * -10)} °C vid −10 °C (${fit.count} punkter, senaste 7 dygnen, ${displayNumber(fit.min_outdoor)} till ${displayNumber(fit.max_outdoor)} °C ute).${outside}`
      : "");
    $("plan-status").textContent = `Uppdaterad ${new Date(data.now * 1000).toLocaleTimeString("sv-SE")}`;
  } catch (error) {
    for (const id of ["plan-outdoor", "plan-price", "plan-offset", "plan-curve", "plan-changes"]) $(id).replaceChildren();
    $("plan-status").textContent = `Kunde inte hämta värmeplanen: ${error.message}`;
  } finally { $("refresh-plan").disabled = false; }
}

const fieldLabels = {
  target_temperature: "Måltemperatur", room_sensors: "Rumsgivare", enabled: "Automatik",
  offset_entity: "Pumpanslutning", outdoor_entity: "Utegivare", price_entity: "Elprisgivare",
  forecast_entity: "Prognoskälla", base_offset: "Basförskjutning", min_offset: "Lägsta förskjutning",
  max_offset: "Högsta förskjutning", weather_gain: "Väderpåverkan", room_gain: "Rumspåverkan",
  price_low: "Lågprisgräns", price_high: "Högprisgräns", cheap_boost: "Höjning vid lågt pris",
  expensive_reduction: "Sänkning vid högt pris", deadband: "Toleransband",
  min_interval_minutes: "Minsta ändringsintervall", max_step: "Största steg", max_age_minutes: "Högsta mätvärdesålder",
};

function logValue(value) {
  if (typeof value === "boolean") return value ? "på" : "av";
  if (Array.isArray(value)) return value.join(", ") || "inga";
  return String(value ?? "saknas");
}

function renderEvents() {
  $("events-list").replaceChildren(...eventRows.map((event) => {
    const entry = document.createElement("article");
    entry.className = "event";
    const heading = document.createElement("strong");
    heading.textContent = event.message;
    const stamp = document.createElement("small");
    stamp.textContent = `${new Date(event.timestamp * 1000).toLocaleString("sv-SE")} · #${event.id}`;
    entry.append(stamp, heading);
    const data = event.details;
    const lines = [];
    for (const [key, change] of Object.entries(data.changes || {})) lines.push(`${fieldLabels[key] || key}: ${logValue(change.from)} → ${logValue(change.to)}`);
    if (event.kind === "write_attempt") lines.push(`Förskjutning ${data.previous} → ${data.value}. Rum ${displayNumber(data.indoor)} °C, mål ${displayNumber(data.target_temperature)} °C. Rumspåverkan ${displayNumber(data.contributions.room)}, väder ${displayNumber(data.contributions.weather)}, pris ${displayNumber(data.contributions.price)}.`);
    if (data.requested !== undefined) lines.push(`Begärt: ${data.requested}. Återläst: ${data.observed}.`);
    if (event.kind === "offset_observed") lines.push(`${data.previous} → ${data.value}. ${data.note}`);
    if (data.error) lines.push(data.error);
    if (data.request_id) lines.push(`Tillhör begäran #${data.request_id}.`);
    if (lines.length) { const p = document.createElement("p"); p.textContent = lines.join("\n"); entry.append(p); }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Tekniska detaljer";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data, null, 2);
    details.append(summary, pre);
    entry.append(details);
    return entry;
  }));
}

async function refreshEvents(older = false) {
  $("refresh-events").disabled = $("older-events").disabled = true;
  try {
    const query = older && eventRows.length ? `?before=${eventRows.at(-1).id}` : "";
    const rows = await api(`events${query}`);
    eventRows = older ? [...eventRows, ...rows] : rows;
    renderEvents();
    $("events-status").textContent = eventRows.length ? `${eventRows.length} händelser visas, nyaste först.` : "Inga händelser sparade ännu.";
    $("older-events").hidden = rows.length < 100;
  } catch (error) {
    $("events-status").textContent = `Kunde inte läsa ändringsloggen: ${error.message}`;
  } finally { $("refresh-events").disabled = $("older-events").disabled = false; }
}

async function refreshDashboard() {
  if (dashboardBusy) return;
  dashboardBusy = true;
  try { await Promise.all([refreshOverview(), refreshEvents()]); }
  finally { dashboardBusy = false; }
}

async function init() {
  makeAdvanced();
  $("save").disabled = $("pause").disabled = true;
  try {
    [settings, entities] = await Promise.all([api("settings"), api("entities")]);
    entities.sort((a, b) => a.name.localeCompare(b.name, "sv"));
    await findPump();
    renderSettings();
    $("save").disabled = $("pause").disabled = false;
  } catch (error) {
    notice(`Kunde inte läsa inställningar och entiteter: ${error.message}. Ladda om sidan för att försöka igen.`, true);
    return;
  }
  $("settings").addEventListener("submit", (event) => {
    event.preventDefault();
    const next = readSettings();
    if (next.enabled && (!settings.enabled || next.offset_entity !== settings.offset_entity)
      && !window.confirm("Har du verifierat att vald entitet är kurvförskjutning och att gränserna passar din pump? Automatiken får ändra denna entitet.")) return;
    void save(next);
  });
  $("pause").addEventListener("click", async () => {
    try {
      const latest = await api("settings");
      await save({ ...latest, enabled: false });
    } catch (error) {
      notice(`Kunde inte stoppa automatiken: ${error.message}`, true);
    }
  });
  for (const [id, direction] of [["target-down", -1], ["target-up", 1]]) {
    $(id).addEventListener("click", () => {
      const input = $("target_temperature");
      const value = Number.isFinite(input.valueAsNumber) ? input.valueAsNumber : settings.target_temperature;
      input.value = Math.max(10, Math.min(30, value + direction * 0.5)).toFixed(1);
    });
  }
  $("offset_entity").addEventListener("change", entityHints);
  $("outdoor_entity").addEventListener("change", entityHints);
  $("price_entity").addEventListener("change", entityHints);
  $("refresh-history").addEventListener("click", () => void refreshHistory());
  $("history-entity").addEventListener("change", () => { if (historyData) renderHistory(); });
  $("refresh-plan").addEventListener("click", () => void refreshPlan());
  $("refresh-events").addEventListener("click", () => void refreshEvents());
  $("older-events").addEventListener("click", () => void refreshEvents(true));
  await refreshStatus();
  void refreshDashboard();
  void refreshHistory();
  void refreshForecast();
  void refreshPlan();
  window.setInterval(refreshStatus, 15000);
  window.setInterval(refreshDashboard, 60000);
  window.setInterval(refreshHistory, 300000);
  window.setInterval(refreshForecast, 1800000);
  window.setInterval(refreshPlan, 300000);
  let lastWidth = window.innerWidth, resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (Math.abs(window.innerWidth - lastWidth) < 40) return;
      lastWidth = window.innerWidth;
      void refreshHistory();
      void refreshPlan();
    }, 400);
  });
}

void init();
