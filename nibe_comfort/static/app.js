"use strict";

const $ = (id) => document.getElementById(id);
const advanced = [
  ["base_offset", "Basförskjutning · steg", -10, 10, 0.1, "Utgångsläget som all kompensation läggs till på."],
  ["min_offset", "Lägsta förskjutning", -10, 10, 0.1, "Absolut nedre säkerhetsgräns."],
  ["max_offset", "Högsta förskjutning", -10, 10, 0.1, "Absolut övre säkerhetsgräns."],
  ["room_gain", "Rumspåverkan · steg/°C", 0, 5, 0.1, "Hur mycket en grads avvikelse påverkar förskjutningen."],
  ["deadband", "Tolerans runt målet · °C", 0, 2, 0.1, "Inom detta band ger rummen ingen extra kompensation."],
  ["max_step", "Maximal ändring per gång", 0.1, 2, 0.1, "Måste vara minst entitetens steglängd."],
  ["min_interval_minutes", "Minst mellan ändringar · minuter", 10, 1440, 1, "Räknas från senaste sparade styrförsök, även efter timeout. Omstart börjar inte om väntetiden."],
  ["max_age_minutes", "Högsta ålder på mätvärden · minuter", 5, 1440, 1, "För gamla eller saknade värden pausar automatiken."],
];
let settings;
let entities = [];
let busy = false;
let discovery = null;
let discoveryError = "";
let dashboardBusy = false;
let eventRows = [];
let eventsError = "";
let lastStatus = null;
let summaryPlan = null;
const heroParts = { plan: [], hotWater: "" };

function renderHeroExtra() {
  $("hero-extra").textContent = [...heroParts.plan, heroParts.hotWater].filter(Boolean).join(" · ");
}
const SVG = "http://www.w3.org/2000/svg";

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = false;
  $("settings-save-status").textContent = message;
  $("settings-save-status").classList.toggle("save-error", error);
}

async function api(path, options = {}) {
  const response = await fetch(`./api/${path}`, { cache: "no-store", ...options, signal: AbortSignal.timeout(20000) });
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
    if (!input || ["room_sensors", "priority_sensors"].includes(key)) continue;
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
    label.className = "room-choice";
    label.dataset.climate = String(climate);
    const title = document.createElement("strong");
    title.textContent = entity.name;
    const measured = document.createElement("span");
    measured.className = "room-choice-temperature";
    measured.textContent = climate ? `${displayNumber(entity.current_temperature)} °C` : `${entity.state} ${entity.unit}`;
    const detail = document.createElement("small");
    const mode = { heat: "värme", auto: "automatik", off: "avstängd", cool: "kyla", heat_cool: "värme/kyla" }[entity.state] || entity.state;
    detail.textContent = climate
      ? `Termostat · ${entity.temperature_error ? "mätvärde saknas" : mode}${entity.setpoint != null ? ` · inställd ${displayNumber(entity.setpoint)} ${entity.temperature_unit || "°C"}` : ""}`
      : "Separat temperaturgivare";
    text.append(title, measured, detail);
    const id = document.createElement("small");
    id.textContent = entity.entity_id;
    text.append(id);
    label.append(input, text);
    $("room_sensors").append(label);
  }
  renderPriorityPicker(settings.priority_sensors);
  renderNearestThermostat(settings.nearest_thermostat);
  filterRoomPicker();
  $("room-impact").open = settings.priority_sensors.length > 0;
  if (!rooms.length) $("room_sensors").textContent = "Inga temperaturgivare hittades i Home Assistant.";
  $("mode").textContent = settings.enabled ? "Automatik aktiverad" : "Övervakning";
  $("mode").classList.toggle("active", settings.enabled);
  $("discovery-message").textContent = discoveryError || discovery?.message || "Automatisk upptäckt är inte tillgänglig.";
  $("manual-pump").open = !settings.offset_entity;
  $("manual-outdoor").open = !settings.outdoor_entity;
  $("manual-price").open = !settings.price_entity;
  if (!settings.room_sensors.length || !settings.offset_entity) $("settings-panel").open = true;
  targetChanged();
  renderIndoorDiff();
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
    if (key === "room_sensors" || key === "priority_sensors") {
      result[key] = Array.from($(key).querySelectorAll("input:checked"), (input) => input.value);
    } else if (typeof value === "boolean") result[key] = $(key).checked;
    else if (typeof value === "number") result[key] = $(key).valueAsNumber;
    else result[key] = $(key).value;
  }
  return result;
}

function renderPriorityPicker(selected) {
  const ids = Array.from($("room_sensors").querySelectorAll("input:checked"), (input) => input.value);
  $("priority_sensors").replaceChildren(...ids.map((id) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    Object.assign(input, { type: "checkbox", value: id, checked: selected.includes(id) });
    const name = document.createElement("span");
    name.textContent = entities.find((e) => e.entity_id === id)?.name || id;
    label.append(input, name);
    return label;
  }));
  limitPriorityPicker();
}

function limitPriorityPicker() {
  const inputs = [...$("priority_sensors").querySelectorAll("input")];
  const full = inputs.filter((input) => input.checked).length >= 2;
  for (const input of inputs) input.disabled = full && !input.checked;
}

function renderNearestThermostat(selected) {
  const ids = Array.from($("room_sensors").querySelectorAll("input:checked"), (input) => input.value)
    .filter((id) => id.startsWith("climate."));
  $("nearest_thermostat").replaceChildren(new Option("Inte markerad", ""),
    ...ids.map((id) => new Option(entities.find((e) => e.entity_id === id)?.name || id, id)));
  $("nearest_thermostat").value = ids.includes(selected) ? selected : "";
}

function filterRoomPicker() {
  const query = $("room-search").value.toLocaleLowerCase("sv-SE");
  const showSensors = $("show-room-sensors").checked;
  const labels = [...$("room_sensors").querySelectorAll("label")];
  for (const label of labels) {
    const checked = label.querySelector("input").checked;
    label.classList.toggle("selected", checked);
    label.hidden = (!showSensors && label.dataset.climate !== "true" && !checked)
      || !label.textContent.toLocaleLowerCase("sv-SE").includes(query);
  }
  const selected = labels.filter((label) => label.querySelector("input").checked);
  const sensors = selected.filter((label) => label.dataset.climate !== "true");
  $("room-selection-count").textContent = `${selected.length - sensors.length} termostater + ${sensors.length} separata givare valda · ${labels.filter((label) => !label.hidden).length} visas`;
  $("selected-room-sensors").textContent = sensors.length
    ? `Valda separata temperaturgivare: ${sensors.map((label) => {
      const id = label.querySelector("input").value;
      return `${entities.find((entity) => entity.entity_id === id)?.name || id} (${id})`;
    }).join(", ")}. Även en termostats temperaturgivare är bara en givare; begränsningen behöver dess climate-entitet. Dessa val följer med när du sparar, även om sökningen döljer dem.`
    : "";
  $("remove-room-sensors").hidden = sensors.length === 0;
}

function roomTrend(room) {
  if (room.error) return room.error;
  const line = historyData?.series.find((line) => line.entity_id === room.entity_id);
  const stamp = (lastStatus?.checked_at || Date.now() / 1000) - 3600;
  const previous = line?.points.findLast((point) => point[0] <= stamp)?.[1];
  if (room.temperature == null || typeof previous !== "number") return "Timförändring saknas";
  const delta = Math.round((room.temperature - previous) * 100) / 100;
  return `${delta > 0 ? "+" : ""}${displayNumber(delta)} °C senaste timmen`;
}

function renderRoomImpact() {
  $("indoor-label").textContent = settings?.priority_sensors.length ? "Prioriterade rum nu" : "Inne nu";
  $("house-indoor").textContent = displayNumber(lastStatus?.house_indoor);
  $("room-impact-list").replaceChildren(...(lastStatus?.rooms || []).map((room) => {
    const row = document.createElement("div");
    row.className = "room-impact-row";
    const name = document.createElement("strong");
    name.textContent = `${room.name}${room.priority ? " · prioriterat" : ""}${room.nearest ? " · närmast pumpen" : ""}`;
    const value = document.createElement("span");
    const diff = room.temperature == null ? null : room.temperature - settings.target_temperature;
    value.textContent = `${displayNumber(room.temperature)} °C${diff == null ? "" : Math.abs(diff) < 0.1 ? " · vid målet" : ` · ${displayNumber(Math.abs(diff))}° ${diff > 0 ? "över" : "under"} målet`}`;
    const trend = document.createElement("small");
    trend.textContent = roomTrend(room);
    row.classList.toggle("too-warm", lastStatus.warm_rooms?.includes(room.entity_id) || false);
    row.append(name, value, trend);
    const thermostat = document.createElement("small");
    const control = lastStatus?.thermostat_control?.rooms.find((entry) => entry.entity_id === room.entity_id);
    thermostat.textContent = [
      room.setpoint != null ? `Termostat inställd på ${displayNumber(room.setpoint)} °C` : room.thermostat_error || "",
      [control?.error || control?.message || "", control?.warning || ""].filter(Boolean).join(" "),
    ].filter(Boolean).join(" · ");
    if (thermostat.textContent) row.append(thermostat);
    return row;
  }));
  const warm = (lastStatus?.rooms || []).filter((room) => lastStatus.warm_rooms?.includes(room.entity_id));
  $("warm-room-warning").hidden = !warm.length;
  $("warm-room-summary").textContent = warm.length
    ? `Varningsgräns ${displayNumber(lastStatus.warm_room_limit)} °C (2° över ditt mål). ${lastStatus.enabled && lastStatus.next_offset > lastStatus.current_offset ? "Automatiken planerar ändå att höja förskjutningen för de prioriterade rummen." : "Följ dessa rum när du värmer de prioriterade rummen."}`
    : "";
  $("warm-room-list").replaceChildren(...warm.map((room) => {
    const item = document.createElement("li");
    item.textContent = `${room.name}: ${displayNumber(room.temperature)} °C · ${displayNumber(room.temperature - settings.target_temperature)}° över målet · ${roomTrend(room)}`;
    return item;
  }));
  renderComfort();
  const control = lastStatus?.thermostat_control;
  $("thermostat-control-status").textContent = lastStatus?.error_message
    ? `Pausad: ${lastStatus.error_message}`
    : control?.enabled ? `Termostatbegränsning på${lastStatus.enabled ? "" : " · huvudautomatiken är av, inga nya ändringar skickas"}.`
      : "Termostatbegränsning av. Inga börvärden ändras.";
  $("thermostat-control-rooms").replaceChildren(...(control?.rooms || []).map((room) => {
    const li = document.createElement("li");
    li.textContent = `${entities.find((e) => e.entity_id === room.entity_id)?.name || room.entity_id}: ${room.error || room.message} ${room.warning || ""}`;
    return li;
  }));
}

function renderComfort() {
  const rooms = lastStatus?.rooms || [];
  const controlling = settings?.priority_sensors.length ? settings.priority_sensors : settings?.room_sensors || [];
  const cold = rooms.filter((room) => controlling.includes(room.entity_id) && room.temperature != null
    && room.temperature < settings.target_temperature - settings.deadband);
  const items = [];
  $("comfort-status").textContent = !rooms.length ? "Aktuella rumstemperaturer saknas."
    : cold.length ? "Dessa styrande rum ligger under målet:"
      : rooms.some((room) => controlling.includes(room.entity_id) && room.temperature == null)
        ? "En styrande givare saknar giltigt mätvärde. Kontrollera anslutningen."
        : "De styrande rummens temperaturer ligger inte under toleransbandet.";
  for (const room of cold) {
    const prefix = `${room.name}: ${displayNumber(room.temperature)} °C, mål ${displayNumber(settings.target_temperature)} °C. `;
    if (room.thermostat_error) items.push(prefix + `Termostatens inställning kan inte bedömas: ${room.thermostat_error}`);
    else if (room.thermostat_mode === "off") items.push(prefix + "Termostaten är avstängd. Kontrollera den innan du höjer värmen till hela huset.");
    else if (typeof room.setpoint === "number" && room.setpoint < settings.target_temperature - 0.1)
      items.push(prefix + `Termostaten är inställd på ${displayNumber(room.setpoint)} °C, under ditt mål. Den kan begränsa värmen. Kontrollera dess inställning innan du höjer förskjutningen.`);
    else if (typeof room.setpoint === "number")
      items.push(prefix + `Termostaten är inställd på ${displayNumber(room.setpoint)} °C. Det visar inte att ventilen är öppen eller att flödet är tillräckligt.`);
    else items.push(prefix + "Termostatens börvärde saknas eller kan inte kopplas entydigt till givaren. Kontrollera radiatorn eller golvvärmen lokalt.");
  }
  if (cold.length && lastStatus?.warm_rooms?.length)
    items.push("Andra rum är redan minst 2 °C över målet. Kontrollera de kalla rummens termostater innan du tillåter mer värme till hela huset.");
  if (cold.length && lastStatus?.control_limit?.direction === "upper")
    items.push("Övre säkerhetsgränsen begränsar förskjutningen. En höjd gräns värmer hela huset och garanterar inte att just detta rum når målet.");
  $("comfort-advice").replaceChildren(...items.map((text) => {
    const li = document.createElement("li"); li.textContent = text; return li;
  }));
  const changes = eventRows.filter((event) => event.kind === "offset_observed"
    && event.details?.entity_id === settings?.offset_entity)
    .sort((a, b) => b.timestamp - a.timestamp);
  const latest = changes[0];
  $("comfort-response").replaceChildren();
  if (eventsError) {
    $("comfort-change").textContent = `Ändringsloggen kunde inte hämtas: ${eventsError}`;
    return;
  }
  if (!latest || latest.details.value <= latest.details.previous) {
    $("comfort-change").textContent = latest
      ? "Senaste bekräftade ändringen var en sänkning. Väntar på nästa höjning för en ny jämförelse."
      : "Ingen bekräftad höjning finns i den hämtade loggen. Ett skickat kommando räcker inte som bekräftelse.";
    return;
  }
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - latest.timestamp) / 60));
  $("comfort-change").textContent = `Förskjutningen ändrades ${displayNumber(latest.details.previous)} → ${displayNumber(latest.details.value)} kl ${clock(latest.timestamp)} (${minutes} minuter sedan). ${minutes < 120 ? "Det är tidigt att bedöma effekten; radiatorer och golvvärme reagerar långsamt." : "Jämför uppmätta temperaturer nedan."}`;
  $("comfort-response").replaceChildren(...rooms.map((room) => {
    const line = historyData?.series.find((line) => line.entity_id === room.entity_id);
    const before = historyData && historyData.start <= latest.timestamp && historyData.end >= latest.timestamp
      ? line?.points.findLast((point) => point[0] <= latest.timestamp)?.[1] : null;
    const li = document.createElement("li");
    if (room.temperature == null || typeof before !== "number" || lastStatus.checked_at < latest.timestamp)
      li.textContent = `${room.name}: jämförelse saknas (${room.error || "historik eller en aktuell avläsning efter höjningen saknas"}).`;
    else {
      const delta = Math.round((room.temperature - before) * 100) / 100;
      li.textContent = `${room.name}: ${displayNumber(before)} → ${displayNumber(room.temperature)} °C (${delta > 0 ? "+" : ""}${displayNumber(delta)} °C sedan höjningen)${room.priority ? " · prioriterat" : ""}.`;
    }
    return li;
  }));
}

function displayNumber(value) {
  return typeof value === "number" ? value.toLocaleString("sv-SE", { maximumFractionDigits: 2 }) : "—";
}

let statusBusy = false;

async function refreshStatus() {
  if (statusBusy) return;
  statusBusy = true;
  try {
    const state = await api("status");
    if (typeof state.enabled === "boolean") {
      $("mode").textContent = state.enabled ? "Automatik aktiverad" : "Övervakning";
      $("mode").classList.toggle("active", state.enabled);
    }
    lastStatus = state;
    renderRoomImpact();
    $("indoor").textContent = displayNumber(state.indoor);
    renderIndoorDiff();
    $("current-offset").textContent = displayNumber(state.current_offset);
    $("hero-offset").textContent = displayNumber(state.current_offset);
    $("hero-offset-base").textContent = typeof settings?.base_offset === "number" ? ` · grundvärde ${displayNumber(settings.base_offset)}` : "";
    $("outdoor-now").textContent = displayNumber(state.outdoor);
    $("outdoor-now").title = state.reading_errors?.outdoor || "";
    $("indoor").title = state.reading_errors?.indoor || "";
    $("hero-offset").title = state.reading_errors?.current_offset || "";
    $("desired-offset").textContent = displayNumber(state.desired_offset);
    $("next-offset").textContent = displayNumber(state.next_offset);
    $("status-title").textContent = state.error_message ? "Styrningen är pausad" : state.enabled ? "Värmen följer dina inställningar" : "Se förslaget innan du styr";
    $("status-message").textContent = state.error_message || state.message;
    if (summaryPlan) renderSummary(summaryPlan);
    else renderControlLimit();
    const checked = state.checked_at ? new Date(state.checked_at * 1000).toLocaleTimeString("sv-SE") : "väntar";
    const stale = !state.checked_at || Date.now() / 1000 - state.checked_at > 180;
    $("updated").textContent = `${stale ? "Avläsningen är inte aktuell. " : ""}Senast avläst: ${checked} · Uppdateras automatiskt${state.wait_seconds ? ` · Nästa ändring tidigast om ${Math.ceil(state.wait_seconds / 60)} min` : ""}`;
    if (state.contributions) {
      const c = state.contributions;
      const shift = state.target_shift ? `${state.target_shift > 0 ? "Förvärmer före dyrt elpris" : "Dyrt elpris"}: målet är tillfälligt ${displayNumber(settings.target_temperature + state.target_shift)} °C. ` : "";
      const note = state.preheat?.note ? `${state.preheat.note} ` : "";
      $("explanation").textContent = `${shift}${note}Bas + rum (${displayNumber(c.room)}) + väder (${displayNumber(c.weather)}) + elpris (${displayNumber(c.price)}) → begränsat mål ${displayNumber(state.desired_offset)}. Nästa steg anpassas till pumpens steglängd och dina gränser.`;
    } else {
      $("explanation").textContent = "Inget aktuellt förslag. Kontrollera inställningarna och statusmeddelandet.";
    }
  } catch (error) {
    $("status-title").textContent = "Kontakt med tillägget saknas";
    $("status-message").textContent = error.message;
    lastStatus = null;
    renderRoomImpact();
    for (const id of ["indoor", "outdoor-now", "current-offset", "hero-offset", "desired-offset", "next-offset"]) $(id).textContent = "—";
    $("hero-offset-base").textContent = "";
    $("indoor-diff").textContent = "Medel av dina valda rum";
    $("updated").textContent = "Visningen är inte aktuell.";
  } finally {
    statusBusy = false;
  }
}

function renderIndoorDiff() {
  const indoor = lastStatus?.indoor, target = settings?.target_temperature;
  const label = settings?.priority_sensors.length ? "Snitt för prioriterade rum" : "Medel av dina valda rum";
  if (typeof indoor !== "number" || typeof target !== "number") { $("indoor-diff").textContent = label; return; }
  const diff = indoor - target;
  $("indoor-diff").textContent = `${label} · ` + (Math.abs(diff) < 0.15 ? "Precis som du vill ha det"
    : `${displayNumber(Math.abs(Math.round(diff * 10) / 10))}° ${diff < 0 ? "under" : "över"} det du vill ha`);
}

function targetChanged() {
  const value = $("target_temperature").valueAsNumber;
  $("save-target").hidden = !settings || !Number.isFinite(value) || Math.abs(value - settings.target_temperature) < 0.01;
  updateSaveState();
}

function updateSaveState() {
  if (!settings) return;
  const next = readSettings();
  const changed = Object.keys(settings).filter((key) => JSON.stringify(next[key]) !== JSON.stringify(settings[key]));
  $("settings-save-status").textContent = changed.length
    ? `Osparade ändringar: ${changed.map((key) => fieldLabels[key] || key).join(", ")}.`
    : "Inga osparade ändringar.";
  $("settings-save-status").classList.remove("save-error");
  $("save-near").disabled = busy;
}

const valueAt = (points, time) => {
  let found = null;
  for (const point of points || []) { if (point[0] <= time && point[1] !== null) found = point; }
  return found;
};

function renderControlLimit() {
  const limit = lastStatus?.control_limit;
  if (!limit || lastStatus.error_message || lastStatus.next_offset !== lastStatus.current_offset) return;
  $("next-change").textContent = limit.direction === "upper" ? "Övre gränsen nådd" : "Nedre gränsen nådd";
  $("next-change-why").textContent = `Beräknad förskjutning ${displayNumber(limit.requested)}, men din säkerhetsgräns är ${displayNumber(limit.value)}. Ingen ytterligare justering tillåts.`;
}

function renderSummary(data) {
  summaryPlan = data;
  const coming = (data.outdoor.forecast || []).filter((point) => point[0] >= data.now && point[0] <= data.now + 86400 && point[1] !== null);
  if (coming.length) {
    const coldest = coming.reduce((a, b) => (b[1] < a[1] ? b : a));
    const warmest = coming.reduce((a, b) => (b[1] > a[1] ? b : a));
    $("outdoor-forecast").textContent = `Närmaste dygnet ${displayNumber(Math.round(coldest[1]))} till ${displayNumber(Math.round(warmest[1]))} °C · kallast ${timeLabel(coldest[0], 86400 * 2)}`;
  } else {
    $("outdoor-forecast").textContent = data.outdoor.error ? "Prognos saknas" : "Ingen prognos vald";
  }
  const next = (data.offset.changes || [])[0];
  const enabled = lastStatus?.enabled ?? settings?.enabled;
  if (next) {
    const now = Math.abs(next.timestamp - data.now) < 120;
    const sameDay = new Date(next.timestamp * 1000).toDateString() === new Date(data.now * 1000).toDateString();
    const clock = new Date(next.timestamp * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
    $("next-change").textContent = now ? "Nu" : sameDay ? `kl ${clock}` : `i morgon ${clock}`;
    const steps = Math.abs(next.offset - next.previous);
    const what = `${next.offset > next.previous ? "Höjer" : "Sänker"} värmen ${steps === 1 ? "ett steg" : `${displayNumber(steps)} steg`} (${next.previous}→${next.offset})`;
    $("next-change-why").textContent = `${enabled ? "" : "Förslag – automatiken är av. "}${what}${next.reasons.length ? ` · ${next.reasons.join(", ")}` : ""}`;
  } else {
    $("next-change").textContent = "Ingen behövs";
    $("next-change-why").textContent = "Ingen ändring av förskjutningen planeras närmaste dygnet";
  }
  const extra = [];
  const price = valueAt(data.prices.points, data.now);
  if (price) {
    const level = price[1] <= data.prices.low ? "billigt" : price[1] >= data.prices.high ? "dyrt" : "normalt";
    extra.push(`Elpris nu ${displayNumber(price[1])}${data.prices.unit ? ` ${data.prices.unit}` : ""} (${level})`);
  }
  heroParts.plan = extra;
  renderHeroExtra();
  renderControlLimit();
}

async function save(config) {
  if (busy) return;
  busy = true;
  $("save").disabled = $("save-near").disabled = $("pause").disabled = $("save-target").disabled = true;
  $("settings-save-status").textContent = "Sparar ändringar…";
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
    void refreshLearning();
  } catch (error) {
    notice(`Kunde inte spara: ${error.message}`, true);
  } finally {
    busy = false;
    $("save").disabled = $("save-near").disabled = $("pause").disabled = $("save-target").disabled = false;
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
const chartRedraws = new WeakMap();

function drawChart(container, series, options) {
  chartRedraws.set(container, () => drawChart(container, series, options));
  const { start, end, unit = "°", now = null, guides = [], markers = [], bands = [], label } = options;
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
  for (const band of bands) {
    if (band.end < start || band.start > end) continue;
    const rect = svgElement("rect", { x: x(band.start), y: top, width: Math.max(2, x(band.end) - x(band.start)), height: height - top - bottom, class: `chart-band ${band.className || ""}` });
    if (band.title) rect.append(svgElement("title", {}, band.title));
    svg.append(rect);
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
  chartRedraws.set(container, () => drawCurve(container, curve));
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

function historyChangeText(change) {
  if (change.status === "observed") {
    return `#${change.id} · ${clock(change.timestamp)} · Kurvförskjutning ${displayNumber(change.previous)} → ${displayNumber(change.value)} återläst. Orsak inte fastställd här; kan vara tillägget, pumptest, manuell ändring eller annan automation. Jämför styrbegäranden i listan.`;
  }
  const name = entities.find((e) => e.entity_id === change.entity_id)?.name || change.entity_id;
  const state = { confirmed: "återläst", sent: "skickad, inte bekräftad", attempt: "begärd, inte bekräftad",
    failed: "anrop misslyckades, kan ändå ha tagits emot", unconfirmed: "återläsning saknas" }[change.status];
  let text = `#${change.id} · ${clock(change.timestamp)} · ${change.kind === "offset" ? "Kurvförskjutning" : name}: ${displayNumber(change.previous)} → ${displayNumber(change.value)} ${change.unit || ""} · ${state}.`;
  if (change.kind === "offset") {
    const c = change.contributions;
    text += c ? ` Beslutsbidrag i kurvsteg: rum ${displayNumber(c.room)}, utetemperatur ${displayNumber(c.weather)}, elpris ${displayNumber(c.price)}.`
      : " Beslutsbidrag saknas i loggen.";
    const values = [
      change.indoor != null ? `inne ${displayNumber(change.indoor)} °C` : "",
      change.target != null ? `mål ${displayNumber(change.target)} °C` : "",
      change.outdoor != null ? `ute ${displayNumber(change.outdoor)} °C` : "",
      change.price != null ? `elpris ${displayNumber(change.price)} ${change.price_unit || "(historisk enhet saknas i loggen)"}` : "",
    ].filter(Boolean);
    if (values.length) text += ` Vid beslutet: ${values.join(", ")}.`;
    if (change.preheat) text += change.preheat === "preheat" ? " Förvärmning aktiv." : " Sparläge vid dyrt elpris aktivt.";
  } else {
    text += ` Övertemperaturbegränsning${change.indoor != null ? `: uppmätt ${displayNumber(change.indoor)} °C` : ""}${change.target != null ? `, mål ${displayNumber(change.target)} °C` : ""}. Elpris används inte direkt för termostatbeslutet.`;
  }
  if (change.outcome_at) text += ` Senaste besked ${clock(change.outcome_at)}.`;
  if (change.error) text += ` ${change.error}`;
  return text;
}

function historyMarkers(changes) {
  return changes.map((change) => ({
    time: change.timestamp,
    label: `${change.kind === "offset" ? "K" : "T"} ${displayNumber(change.value)} · ${["confirmed", "observed"].includes(change.status) ? "återläst" : "begäran"}`,
    short: `${change.kind === "offset" ? "K" : "T"}${change.id}`,
    className: change.status === "confirmed" ? "done-auto" : "done-other",
    title: historyChangeText(change),
  }));
}

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
  const changes = (data.changes || []).filter((change) => change.kind === "offset" || !single || change.entity_id === single.entity_id);
  drawChart($("temperature-chart"), series, { start: data.start, end: data.end, guides, markers: historyMarkers(changes) });
  legend($("temperature-legend"), series);
  $("history-changes-status").textContent = data.changes_error || (changes.length
    ? `${changes.length} loggade begäranden/observationer under perioden. Begäran och återläst kurvändring kan vara separata poster. Bidrag 0 betyder ingen direkt påverkan vid beslutet.`
    : "Inga styrbegäranden eller kurvobservationer finns loggade för dessa rum och denna pump under perioden.");
  $("history-changes").replaceChildren(...changes.map((change) => {
    const item = document.createElement("li");
    item.className = change.status === "confirmed" ? "done-auto" : "done-other";
    item.textContent = historyChangeText(change);
    return item;
  }));
  $("thermostat-charts").replaceChildren();
  const charts = [];
  for (const line of (single ? [single] : lines).filter((entry) => entry.kind === "climate")) {
    const card = document.createElement("section");
    card.className = "thermostat-history";
    const title = document.createElement("h3");
    title.textContent = line.name;
    const chart = document.createElement("div");
    chart.className = "chart";
    chart.setAttribute("role", "img");
    chart.setAttribute("aria-label", `${line.name}: uppmätt temperatur, börvärde och styrbegäranden senaste dygnet`);
    const roomSeries = [
      { name: "Uppmätt °C", points: line.points, until: data.end, color: 0 },
      { name: "Termostatens börvärde °C", points: line.setpoints || [], until: data.end, color: 1, dashed: true },
    ];
    const roomLegend = document.createElement("div");
    roomLegend.className = "chart-legend";
    card.append(title, chart, roomLegend);
    $("thermostat-charts").append(card);
    charts.push({ chart, roomSeries, line });
    legend(roomLegend, roomSeries);
  }
  for (const { chart, roomSeries, line } of charts) {
    drawChart(chart, roomSeries, { start: data.start, end: data.end,
      markers: historyMarkers(changes.filter((change) => change.kind === "offset" || change.entity_id === line.entity_id)) });
  }
  $("thermostat-charts-title").hidden = charts.length === 0;
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
    renderRoomImpact();
  } catch (error) {
    historyData = null;
    renderRoomImpact();
    $("temperature-chart").replaceChildren();
    $("temperature-legend").replaceChildren();
    $("history-table").replaceChildren();
    $("history-changes").replaceChildren();
    $("thermostat-charts").replaceChildren();
    $("thermostat-charts-title").hidden = true;
    $("history-changes-status").textContent = "Ändringshistoriken är inte tillgänglig.";
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
    renderSummary(data);

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

const hourRange = (from, to) => `${String(from).padStart(2, "0")}–${String((to + 1) % 24).padStart(2, "0")}`;

// Hours that together hold most events, merged into readable ranges like "05–07".
function busyHours(events) {
  const counts = Array(24).fill(0);
  for (const event of events) counts[new Date(event.start * 1000).getHours()]++;
  const threshold = Math.max(2, Math.max(...counts) / 2);
  const ranges = [];
  for (let hour = 0; hour < 24; hour++) {
    if (counts[hour] < threshold && !(counts[hour] && (counts[hour - 1] >= threshold || counts[hour + 1] >= threshold))) continue;
    const last = ranges.at(-1);
    if (last && last[1] === hour - 1) last[1] = hour; else ranges.push([hour, hour]);
  }
  return ranges.map(([from, to]) => hourRange(from, to));
}

function drawTank(container, data) {
  container.replaceChildren();
  const width = 130, height = 230, inner = { x: 22, y: 26, w: 86, h: 182 };
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  svg.append(svgElement("rect", { x: inner.x - 6, y: inner.y - 8, width: inner.w + 12, height: inner.h + 16, rx: 26, class: "hw-shell" }));
  const level = typeof data.level === "number" ? data.level : null;
  const clip = svgElement("clipPath", { id: "hw-clip" });
  clip.append(svgElement("rect", { x: inner.x, y: inner.y, width: inner.w, height: inner.h, rx: 20 }));
  svg.append(clip);
  svg.append(svgElement("rect", { x: inner.x, y: inner.y, width: inner.w, height: inner.h, rx: 20, class: "hw-empty" }));
  if (level !== null) {
    const fill = inner.h * level;
    const tone = level >= 0.5 ? "hw-hot" : level >= 0.25 ? "hw-warm" : "hw-cold";
    svg.append(svgElement("rect", { x: inner.x, y: inner.y + inner.h - fill, width: inner.w, height: fill, class: `hw-fill ${tone}`, "clip-path": "url(#hw-clip)" }));
    svg.append(svgElement("text", { x: width / 2, y: inner.y + inner.h / 2 + 8, "text-anchor": "middle", class: "hw-percent" }, `${Math.round(level * 100)}%`));
  } else {
    svg.append(svgElement("text", { x: width / 2, y: inner.y + inner.h / 2 + 6, "text-anchor": "middle", class: "hw-percent small" }, "okänt"));
  }
  if (typeof data.top === "number") svg.append(svgElement("text", { x: width / 2, y: inner.y + 22, "text-anchor": "middle", class: "hw-label" }, `topp ${displayNumber(data.top)}°`));
  if (typeof data.charge === "number") svg.append(svgElement("text", { x: width / 2, y: inner.y + inner.h - 14, "text-anchor": "middle", class: "hw-label" }, `BT6 ${displayNumber(data.charge)}°`));
  if (data.charging) svg.append(svgElement("text", { x: width / 2, y: 16, "text-anchor": "middle", class: "hw-charging" }, "⚡ laddar"));
  container.append(svg);
}

function drawHourBars(container, events) {
  container.replaceChildren();
  if (!events.length) { container.textContent = "Inga tappningar hittades senaste veckan."; return; }
  const big = Array(24).fill(0), small = Array(24).fill(0);
  for (const event of events) (event.size === "stor" ? big : small)[new Date(event.start * 1000).getHours()]++;
  const maximum = Math.max(1, ...big.map((value, hour) => value + small[hour]));
  const width = chartWidth(container), narrow = width < 520, height = narrow ? 150 : 170;
  const left = narrow ? 26 : 32, right = 6, top = 10, bottom = 24;
  const slot = (width - left - right) / 24, bar = Math.max(3, slot - (narrow ? 2 : 4));
  const y = (value) => top + (1 - value / maximum) * (height - top - bottom);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  for (const value of [0, maximum]) {
    svg.append(svgElement("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "chart-grid" }));
    svg.append(svgElement("text", { x: left - 5, y: y(value) + 4, "text-anchor": "end" }, String(value)));
  }
  for (let hour = 0; hour < 24; hour++) {
    const x = left + hour * slot + (slot - bar) / 2;
    if (small[hour]) svg.append(svgElement("rect", { x, y: y(small[hour] + big[hour]), width: bar, height: y(big[hour]) - y(small[hour] + big[hour]), rx: 2, class: "hw-bar small" }));
    if (big[hour]) svg.append(svgElement("rect", { x, y: y(big[hour]), width: bar, height: y(0) - y(big[hour]), rx: 2, class: "hw-bar big" }));
    const total = big[hour] + small[hour];
    if (total) svg.lastChild.append(svgElement("title", {}, `kl ${hourRange(hour, hour)}: ${total} tappningar`));
    if (hour % (narrow ? 6 : 3) === 0) svg.append(svgElement("text", { x: left + hour * slot + slot / 2, y: height - 7, "text-anchor": "middle" }, String(hour).padStart(2, "0")));
  }
  container.append(svg);
}

async function refreshHotWater() {
  $("refresh-hot-water").disabled = true;
  try {
    const data = await api("hotwater");
    if (data.error) {
      $("hw-status").textContent = data.error;
      for (const id of ["hw-tank", "hw-metrics", "hw-hours", "hw-chart", "hw-legend", "hw-list"]) $(id).replaceChildren();
      $("hw-verdict").textContent = "Varmvatten saknas";
      $("hw-verdict-text").textContent = $("hw-pattern").textContent = "";
      heroParts.hotWater = "";
      renderHeroExtra();
      return;
    }
    drawTank($("hw-tank"), data);
    const level = data.level;
    const verdict = level === null ? "Nivån går inte att bedöma" : level >= 0.8 ? "Gott om varmvatten"
      : level >= 0.5 ? "Varmvattnet räcker bra" : level >= 0.25 ? "Varmvattnet börjar ta slut" : "Nästan slut på varmvatten";
    $("hw-verdict").textContent = verdict;
    const charges = data.charges || [];
    const lastCharge = charges.filter((charge) => charge.end < data.now - 60).at(-1);
    const clock = (stamp) => timeLabel(stamp, 86400 * 2);
    const facts = [];
    if (data.level_source === "minutes") facts.push(`Pumpen uppskattar ungefär ${displayNumber(data.minutes)} minuter varmvatten kvar (fullt brukar vara ${displayNumber(data.max_minutes)}).`);
    else if (data.level_source === "charge") facts.push(`Bedömt från laddgivaren BT6 (${displayNumber(data.charge)} °C) jämfört med hur varm tanken brukar bli.`);
    facts.push(data.charging ? "Pumpen laddar varmvatten just nu." : lastCharge ? `Senast laddat ${clock(lastCharge.end)}.` : "");
    if (!data.charging && level !== null && level < 0.35) facts.push("Pumpen brukar börja ladda när det behövs.");
    $("hw-verdict-text").textContent = facts.filter(Boolean).join(" ");
    const metrics = [
      { label: "Toppen (BT7)", value: typeof data.top === "number" ? displayNumber(data.top) : null, unit: "°C" },
      { label: "Laddgivare (BT6)", value: typeof data.charge === "number" ? displayNumber(data.charge) : null, unit: "°C" },
      { label: "Kvar enligt pumpen", value: typeof data.minutes === "number" ? displayNumber(data.minutes) : null, unit: "min" },
      { label: "Inställt behov", value: data.demand ?? null, unit: "" },
    ].filter((item) => item.value !== null);
    $("hw-metrics").replaceChildren(...metrics.map(metric));
    heroParts.hotWater = level === null ? "" : `Varmvatten ${Math.round(level * 100)}%${typeof data.minutes === "number" ? ` (${displayNumber(data.minutes)} min)` : ""}`;
    renderHeroExtra();

    const events = data.draw_offs || [];
    const days = data.days || 7;
    const bigOnes = events.filter((event) => event.size === "stor");
    const pattern = [];
    if (events.length) {
      pattern.push(`Senaste ${days} dygnen: ${events.length} tappningar (cirka ${displayNumber(Math.round(events.length / days * 10) / 10)} per dygn), varav ${bigOnes.length} stora som dusch eller bad.`);
      const bigHours = busyHours(bigOnes);
      if (bigHours.length) pattern.push(`Stora tappningar sker oftast kl ${bigHours.join(" och ")}.`);
      const allHours = busyHours(events);
      if (allHours.length && allHours.join() !== bigHours.join()) pattern.push(`Alla tappningar: oftast kl ${allHours.join(", ")}.`);
      const recovery = bigOnes.map((event) => charges.find((charge) => charge.end > event.end && charge.start - event.end < 4 * 3600))
        .map((charge, index) => charge && (charge.end - bigOnes[index].end) / 60).filter((value) => value > 0).sort((a, b) => a - b);
      if (recovery.length) pattern.push(`Efter en stor tappning är tanken normalt laddad igen inom ungefär ${Math.round(recovery[Math.floor(recovery.length / 2)])} minuter.`);
    } else {
      pattern.push(`Inga tydliga tappningar hittades senaste ${days} dygnen.`);
    }
    if (charges.length) pattern.push(`Pumpen laddade varmvatten ${charges.length} gånger (cirka ${displayNumber(Math.round(charges.length / days * 10) / 10)} per dygn).`);
    $("hw-pattern").textContent = pattern.join(" ");
    drawHourBars($("hw-hours"), events);

    const start = data.now - 2 * 86400;
    const clip = (points) => points.filter((point, index) => point[0] >= start || points[index + 1]?.[0] > start).map(([t, v]) => [Math.max(start, t), v]);
    const series = [
      { name: "Toppen (BT7)", points: clip(data.series.top || []), until: data.now, color: 1 },
      { name: "Laddgivare (BT6)", points: clip(data.series.charge || []), until: data.now, color: 4 },
    ].filter((line) => line.points.length);
    const shortTime = (stamp) => new Date(stamp * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
    drawChart($("hw-chart"), series, {
      start, end: data.now, now: data.now, empty: "Ingen historik för varmvattnet.",
      bands: charges.map((charge) => ({ start: charge.start, end: charge.end, className: "hw-charge", title: `Laddar ${shortTime(charge.start)}–${shortTime(charge.end)}` })),
      markers: events.filter((event) => event.drop >= 4).map((event) => ({
        time: event.start, className: "hw-draw", label: `${shortTime(event.start)} −${displayNumber(event.drop)}°`, short: `−${Math.round(event.drop)}°`,
        title: `${event.size} tappning: BT6 ${displayNumber(event.from)} → ${displayNumber(event.to)} °C`,
      })),
    });
    legend($("hw-legend"), series);
    const recent = events.slice(-8).reverse();
    $("hw-list").replaceChildren(...(recent.length ? recent.map((event) => {
      const item = document.createElement("li");
      item.className = `hw-${event.size === "stor" ? "big" : "small"}`;
      const strong = document.createElement("strong");
      strong.textContent = `${timeLabel(event.start, 86400 * 2)}: ${event.size} tappning`;
      item.append(strong, ` · BT6 ${displayNumber(event.from)} → ${displayNumber(event.to)} °C`);
      return item;
    }) : [Object.assign(document.createElement("li"), { className: "muted", textContent: "Inga tappningar hittade." })]));
    $("hw-status").textContent = `Uppdaterad ${new Date(data.now * 1000).toLocaleTimeString("sv-SE")}. Bedömningen bygger på pumpens givare och är ungefärlig.`;
  } catch (error) {
    $("hw-status").textContent = `Kunde inte läsa varmvattnet: ${error.message}`;
  } finally { $("refresh-hot-water").disabled = false; }
}

function progressRow(label, value, needed, text) {
  const row = document.createElement("div");
  row.className = "learn-row";
  const title = document.createElement("span");
  title.textContent = label;
  const bar = document.createElement("div");
  bar.className = "learn-bar";
  const fill = document.createElement("i");
  fill.style.width = `${Math.round(Math.max(0, Math.min(1, value / needed)) * 100)}%`;
  bar.append(fill);
  const small = document.createElement("small");
  small.textContent = text;
  row.append(title, bar, small);
  return row;
}

async function refreshLearning() {
  $("refresh-learning").disabled = true;
  try {
    const data = await api("learning");
    const model = data.model;
    const titles = { learning: "Lär sig fortfarande", rough: "Grov bild av huset", good: "Känner huset väl" };
    $("learn-status").textContent = data.error || (model ? `${titles[model.status]} · uppdaterad ${new Date(data.now * 1000).toLocaleTimeString("sv-SE")}` : "Ingen modell ännu.");
    $("learn-message").textContent = model?.message || "";
    $("learn-message").hidden = !model?.message;
    $("learn-progress").replaceChildren(...(model ? [
      progressRow("Mätvärden", model.hours, model.needed_hours, `${displayNumber(model.days)} av 7 dygn`),
      progressRow("Kurvändringar att lära av", model.changes, model.needed_changes, `${model.changes} av ${model.needed_changes}`),
    ] : []));
    const facts = model?.facts;
    $("learn-facts").replaceChildren(...(facts ? [
      { label: "Ett steg på kurvan ger", value: `${facts.degrees_per_step >= 0 ? "+" : ""}${displayNumber(facts.degrees_per_step)}`, unit: "°C inne på sikt" },
      { label: "Halva effekten märks efter", value: `≈ ${displayNumber(facts.half_response_hours)}`, unit: "timmar" },
      { label: "Efter 3 timmar", value: `${displayNumber(facts.step_after_3h)}`, unit: "°C per steg" },
    ].map(metric) : []));

    const preheat = data.preheat;
    const windows = preheat.windows || [];
    const start = data.now - 24 * 3600, end = data.now + 24 * 3600;
    const series = [
      { name: settings.priority_sensors.length ? "Inne, prioriterade rum (timvis)" : "Inne, medel av rummen (timvis)", points: data.history, color: 0, step: false },
      { name: "Förutsägelse", points: data.prediction, color: 2, dashed: true, step: false },
    ].filter((line) => line.points.length);
    const clock = (stamp) => new Date(stamp * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
    drawChart($("learn-chart"), series, {
      start, end, now: data.now, integer: false, pad: 0.3,
      empty: model?.status === "learning" ? "Förutsägelsen visas när modellen har lärt sig tillräckligt." : "Ingen innetemperatur att visa ännu.",
      guides: [{ value: data.target, label: `Mål ${displayNumber(data.target)}°` }],
      bands: windows.map((w) => ({ start: w.start, end: w.end, className: w.kind === "preheat" ? "preheat" : "coast",
        title: `${w.kind === "preheat" ? "Förvärmning" : "Dyrt elpris"} ${clock(w.start)}–${clock(w.end)}` })),
    });
    legend($("learn-legend"), series);
    const items = windows.map((w) => {
      const item = document.createElement("li");
      item.className = w.kind === "preheat" ? "preheat" : "coast";
      const strong = document.createElement("strong");
      strong.textContent = `${timeLabel(w.start, 86400 * 2)}–${clock(w.end)}: ${w.kind === "preheat" ? `förvärm +${displayNumber(w.shift)}°` : `låt bli ${displayNumber(-w.shift)}° svalare`}`;
      item.append(strong, ` · elpris ${displayNumber(w.price)}`);
      return item;
    });
    const lead = preheat.lead_hours ? `Förvärmningen börjar ${preheat.lead_hours} timmar före${preheat.lead_source === "learned" ? " (inlärt)" : " (standard tills modellen är säker)"}.` : "";
    const state = preheat.enabled ? "Förvärmning är på." : "Förvärmning är av – så här skulle den göra:";
    if (!items.length) items.push(Object.assign(document.createElement("li"), { className: "muted", textContent: preheat.note || "Inga förvärmningar planerade." }));
    const head = Object.assign(document.createElement("li"), { className: "plan-heading", textContent: `${state} ${lead}`.trim() });
    $("learn-windows").replaceChildren(head, ...items);
  } catch (error) {
    $("learn-status").textContent = `Kunde inte läsa husmodellen: ${error.message}`;
  } finally { $("refresh-learning").disabled = false; }
}

const fieldLabels = {
  target_temperature: "Måltemperatur", room_sensors: "Rumsgivare", priority_sensors: "Prioriterade rum", enabled: "Automatik",
  nearest_thermostat: "Närmast värmepumpen", thermostat_control_enabled: "Termostatbegränsning",
  offset_entity: "Pumpanslutning", outdoor_entity: "Utegivare", price_entity: "Elprisgivare",
  forecast_entity: "Prognoskälla", base_offset: "Basförskjutning", min_offset: "Lägsta förskjutning",
  max_offset: "Högsta förskjutning", weather_gain: "Väderpåverkan", room_gain: "Rumspåverkan",
  price_low: "Lågprisgräns", price_high: "Högprisgräns", cheap_boost: "Höjning vid lågt pris",
  expensive_reduction: "Sänkning vid högt pris", deadband: "Toleransband",
  min_interval_minutes: "Minsta ändringsintervall", max_step: "Största steg", max_age_minutes: "Högsta mätvärdesålder",
  preheat_enabled: "Förvärmning", preheat_degrees: "Förvärmning · grader",
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
    if (event.kind === "write_attempt") lines.push(`Förskjutning ${data.previous} → ${data.value}. Rum ${displayNumber(data.indoor)} °C, mål ${displayNumber(data.target_temperature)} °C. Rumspåverkan ${displayNumber(data.contributions.room)}, väder ${displayNumber(data.contributions.weather)}, pris ${displayNumber(data.contributions.price)}.${data.target_shift ? ` Mål tillfälligt ${data.target_shift > 0 ? "+" : ""}${displayNumber(data.target_shift)}° (${data.preheat === "preheat" ? "förvärmning" : "dyrt elpris"}).` : ""}`);
    if (data.requested !== undefined) lines.push(`Begärt: ${data.requested}. Återläst: ${data.observed}.`);
    if (event.kind === "test_write") lines.push(`Förskjutning ${data.previous} → ${data.value} i ${data.restore_after_minutes} minuter.`);
    if (event.kind === "test_restore") lines.push(`Tillbaka till ${data.value}.`);
    if (event.kind.startsWith("thermostat_")) {
      if (data.entity_id) lines.push(`Termostat: ${data.entity_id}.`);
      if (data.value !== undefined) lines.push(`Börvärde: ${data.previous !== undefined ? `${data.previous} → ` : ""}${data.value}${data.unit ? ` ${data.unit}` : ""}.`);
    }
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
    eventsError = "";
    renderEvents();
    renderComfort();
    $("events-status").textContent = eventRows.length ? `${eventRows.length} händelser visas, nyaste först.` : "Inga händelser sparade ännu.";
    $("older-events").hidden = rows.length < 100;
  } catch (error) {
    eventsError = error.message;
    renderComfort();
    $("events-status").textContent = `Kunde inte läsa ändringsloggen: ${error.message}`;
  } finally { $("refresh-events").disabled = $("older-events").disabled = false; }
}

async function refreshDashboard() {
  if (dashboardBusy) return;
  dashboardBusy = true;
  try { await Promise.all([refreshOverview(), refreshEvents()]); }
  finally { dashboardBusy = false; }
}

const clock = (time) => new Date(time * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
let testTimer = null;

function renderTest(data) {
  const test = data.test;
  const active = Boolean(test && ["running", "restoring"].includes(test.phase));
  $("test-start").hidden = active;
  $("test-stop").hidden = !test || test.phase !== "running";
  $("test-check").hidden = !test || test.result === "failed";
  if (test) {
    const now = lastStatus?.current_offset;
    $("test-before").textContent = displayNumber(test.original);
    $("test-during").textContent = displayNumber(test.value);
    $("test-now").textContent = now == null ? "—" : displayNumber(now);
    $("test-look").innerHTML = test.phase === "running"
      ? `Titta på pumpen nu: förskjutningen ska visa <b>${displayNumber(test.value)}</b>.`
      : `Förskjutningen ska nu visa <b>${displayNumber(test.original)}</b> igen.`;
    const base = settings?.base_offset;
    $("test-base-note").hidden = base == null || Math.abs(base - test.original) < 1e-6;
    $("test-base-note").textContent = `Ditt grundvärde är ${displayNumber(base)}, men automatiken hade justerat förskjutningen till ${displayNumber(test.original)} när testet startade. Därför utgår testet från ${displayNumber(test.original)}.`;
  }
  if (!test) {
    $("test-steps").replaceChildren();
    $("test-status").textContent = "Inget test har körts ännu.";
    return;
  }
  const failed = test.result === "failed";
  const restored = test.phase === "done" && ["confirmed", "unconfirmed"].includes(test.result);
  const steps = [
    [failed ? "fail" : "done", `Ändring skickad: ${displayNumber(test.original)} → ${displayNumber(test.value)} kl ${clock(test.started_at)}`],
    [test.observed_at ? "done" : test.phase === "running" ? "wait" : "fail",
      test.observed_at ? `Pumpen rapporterar ${displayNumber(test.value)} via Home Assistant (kl ${clock(test.observed_at)})`
        : test.phase === "running" ? "Väntar på att pumpen rapporterar det nya värdet… (brukar ta 1–5 minuter)" : "Pumpen rapporterade aldrig det nya värdet"],
    [test.phase === "running" ? "now" : test.observed_at ? "done" : "skip", `Kontrollera på pumpen att förskjutningen visar ${displayNumber(test.value)}`],
    [restored ? "done" : test.phase === "restoring" ? "wait" : test.phase === "running" ? "todo" : "fail",
      restored ? `Återställd till ${displayNumber(test.original)}` : test.phase === "restoring" ? `Återställer till ${displayNumber(test.original)}…`
        : test.phase === "running" ? `Återställs automatiskt till ${displayNumber(test.original)} kl ${clock(test.restore_at)}` : "Återställningen kunde inte bekräftas"],
  ];
  $("test-steps").replaceChildren(...steps.map(([state, text]) => {
    const item = document.createElement("li");
    item.className = state;
    item.textContent = text;
    return item;
  }));
  const minutes = Math.max(0, Math.ceil((test.restore_at - data.now) / 60));
  $("test-status").textContent = {
    running: `Testet pågår. ${minutes} min kvar innan förskjutningen ställs tillbaka.`,
    restoring: "Testtiden är slut. Väntar på att pumpen rapporterar ursprungsvärdet.",
  }[test.phase] || {
    confirmed: "Klart! Pumpen tog emot ändringen och är återställd. Styrningen fungerar.",
    unconfirmed: "Pumpen rapporterade aldrig testvärdet. Ändringen når troligen inte pumpen; se tipsen nedan.",
    changed: "Förskjutningen ändrades av något annat under testet och lämnades orörd. Kontrollera värdet på pumpen.",
    restore_unconfirmed: `Återställningen kunde inte bekräftas. Kontrollera att förskjutningen är ${displayNumber(test.original)} på pumpen.`,
    failed: `Home Assistant tog inte emot ändringen: ${test.error || "okänt fel"}.`,
  }[test.result];
  window.clearTimeout(testTimer);
  if (active) testTimer = window.setTimeout(refreshTest, 10000);
}

async function refreshTest() {
  try {
    const data = await api("test");
    if (data.test && ["running", "restoring"].includes(data.test.phase)) await refreshStatus();
    renderTest(data);
  }
  catch (error) { $("test-status").textContent = `Kunde inte läsa teststatus: ${error.message}`; }
}

async function testAction(action) {
  if (action === "start" && !window.confirm("Förskjutningen ändras ett steg i 10 minuter och ställs sedan tillbaka automatiskt. Starta testet?")) return;
  $("test-start").disabled = $("test-stop").disabled = true;
  try {
    renderTest(await api("test", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) }));
    void refreshEvents();
  } catch (error) {
    $("test-status").textContent = `Testet kunde inte ${action === "start" ? "startas" : "avbrytas"}: ${error.message}`;
  } finally { $("test-start").disabled = $("test-stop").disabled = false; }
}

async function showVersion() {
  try {
    const { version } = await api("version");
    $("version").textContent = ` · Version ${version}`;
  } catch {
    $("version").textContent = "";
  }
}

async function init() {
  makeAdvanced();
  $("save").disabled = $("save-near").disabled = $("pause").disabled = true;
  try {
    [settings, entities] = await Promise.all([api("settings"), api("entities")]);
    entities.sort((a, b) => a.name.localeCompare(b.name, "sv"));
    await findPump();
    renderSettings();
    $("save").disabled = $("save-near").disabled = $("pause").disabled = false;
    $("notice").hidden = true;
  } catch (error) {
    notice(`Kunde inte läsa inställningar och entiteter: ${error.message}. Försöker igen automatiskt om 15 sekunder.`, true);
    window.setTimeout(init, 15000);
    return;
  }
  $("settings").addEventListener("submit", (event) => {
    event.preventDefault();
    const next = readSettings();
    if (next.thermostat_control_enabled
      && (!next.room_sensors.length || next.room_sensors.some((id) => !id.startsWith("climate.")))) {
      $("room-search").value = "";
      $("show-room-sensors").checked = true;
      filterRoomPicker();
      const sensors = next.room_sensors.filter((id) => !id.startsWith("climate."));
      notice(sensors.length
        ? `Kunde inte spara: följande val är separata temperaturgivare, inte styrbara termostater: ${sensors.join(", ")}. Avmarkera dem under Rumsgivare och välj climate-termostaterna.`
        : "Kunde inte spara: välj minst en climate-termostat för termostatbegränsning.", true);
      $("room_sensors").scrollIntoView({ block: "center" });
      return;
    }
    if (next.thermostat_control_enabled && (!settings.thermostat_control_enabled
      || next.target_temperature !== settings.target_temperature
      || JSON.stringify(next.room_sensors) !== JSON.stringify(settings.room_sensors))
      && !window.confirm("Valda termostaters högre börvärden kan sänkas till ditt rumsmål vid minst 0,5 °C övertemperatur. Tillräckligt flöde måste finnas i anläggningen. Avstängning återställer inte redan ändrade börvärden. Godkänner du termostatbegränsningen för dessa rum och detta mål?")) return;
    if (next.enabled && JSON.stringify(next.priority_sensors) !== JSON.stringify(settings.priority_sensors)
      && !window.confirm("Styrningen kommer att följa snittet för de prioriterade rummen, eller alla valda rum om inget prioriteras. Kurvförskjutningen påverkar hela huset och andra rum kan bli varmare. Spara ändringen?")) return;
    if (next.enabled && (!settings.enabled || next.offset_entity !== settings.offset_entity)
      && !window.confirm("Har du verifierat att vald entitet är kurvförskjutning och att gränserna passar din pump? Automatiken får ändra denna entitet.")) return;
    void save(next);
  });
  $("settings").addEventListener("input", updateSaveState);
  $("settings").addEventListener("change", updateSaveState);
  document.querySelectorAll(".page-nav a").forEach((link) => link.addEventListener("click", () => {
    const target = document.querySelector(link.getAttribute("href"));
    if (target?.tagName === "DETAILS") target.open = true;
  }));
  document.addEventListener("toggle", (event) => {
    if (event.target.tagName !== "DETAILS" || !event.target.open) return;
    window.requestAnimationFrame(() => {
      for (const chart of event.target.querySelectorAll(".chart")) {
        if (chart.getClientRects().length && chart.querySelector("svg")) chartRedraws.get(chart)?.();
      }
    });
  }, true);
  document.addEventListener("invalid", (event) => {
    for (let parent = event.target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
  }, true);
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
      targetChanged();
    });
  }
  $("target_temperature").addEventListener("input", targetChanged);
  $("room_sensors").addEventListener("change", () => {
    renderPriorityPicker(Array.from($("priority_sensors").querySelectorAll("input:checked"), (input) => input.value));
    renderNearestThermostat($("nearest_thermostat").value);
    filterRoomPicker();
  });
  $("room-search").addEventListener("input", filterRoomPicker);
  $("show-room-sensors").addEventListener("change", filterRoomPicker);
  $("remove-room-sensors").addEventListener("click", () => {
    for (const input of $("room_sensors").querySelectorAll("input:checked")) {
      if (!input.value.startsWith("climate.")) input.checked = false;
    }
    $("room_sensors").dispatchEvent(new Event("change", { bubbles: true }));
  });
  $("priority_sensors").addEventListener("change", limitPriorityPicker);
  $("offset_entity").addEventListener("change", entityHints);
  $("outdoor_entity").addEventListener("change", entityHints);
  $("price_entity").addEventListener("change", entityHints);
  $("refresh-history").addEventListener("click", () => void refreshHistory());
  $("history-entity").addEventListener("change", () => { if (historyData) renderHistory(); });
  $("refresh-plan").addEventListener("click", () => void refreshPlan());
  $("refresh-hot-water").addEventListener("click", () => void refreshHotWater());
  $("refresh-learning").addEventListener("click", () => void refreshLearning());
  $("refresh-events").addEventListener("click", () => void refreshEvents());
  $("test-start").addEventListener("click", () => void testAction("start"));
  $("test-stop").addEventListener("click", () => void testAction("stop"));
  $("older-events").addEventListener("click", () => void refreshEvents(true));
  await refreshStatus();
  void showVersion();
  void refreshTest();
  void refreshDashboard();
  void refreshHistory();
  void refreshForecast();
  void refreshPlan();
  void refreshHotWater();
  void refreshLearning();
  window.setInterval(refreshStatus, 15000);
  window.setInterval(refreshLearning, 900000);
  window.setInterval(refreshHotWater, 120000);
  window.setInterval(refreshDashboard, 60000);
  window.setInterval(refreshHistory, 300000);
  window.setInterval(refreshForecast, 1800000);
  window.setInterval(refreshPlan, 300000);
  const resumeDashboard = () => {
    if (document.visibilityState === "hidden") return;
    void refreshStatus();
    void refreshDashboard();
    void refreshPlan();
    void refreshHotWater();
    void refreshTest();
  };
  document.addEventListener("visibilitychange", resumeDashboard);
  window.addEventListener("pageshow", resumeDashboard);
  window.addEventListener("online", resumeDashboard);
  let lastWidth = window.innerWidth, resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (Math.abs(window.innerWidth - lastWidth) < 40) return;
      lastWidth = window.innerWidth;
      void refreshHistory();
      void refreshPlan();
      void refreshHotWater();
      void refreshLearning();
    }, 400);
  });
}

void init();
