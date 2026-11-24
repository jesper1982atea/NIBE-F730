"use strict";

const $ = (id) => document.getElementById(id);

// The page refreshes itself every 15 s. Rebuilding a <select> closes it while open (Safari, HA app), and swapping
// a button between mousedown and mouseup loses the click. These helpers only touch the DOM when something changed.
function fillSelect(select, options, value) {
  const wanted = options.map((option) => [option.value, option.text]);
  const current = [...select.options].map((option) => [option.value, option.text]);
  if (document.activeElement !== select && JSON.stringify(wanted) !== JSON.stringify(current)) select.replaceChildren(...options);
  if (document.activeElement !== select && select.value !== value) select.value = value;
}

// True while a settings field has focus: rebuilding the form then closes an open list or swallows a click.
function editingSettings() {
  const active = document.activeElement;
  return Boolean(active && active !== document.body && active.closest("#settings, #settings-panel")
    && /^(SELECT|INPUT|TEXTAREA)$/.test(active.tagName));
}

function replaceIfChanged(container, children) {
  const scratch = document.createElement(container.tagName);
  scratch.append(...children);
  if (scratch.innerHTML === container.innerHTML) return false;
  container.replaceChildren(...scratch.childNodes);
  return true;
}
const advanced = [
  ["base_offset", "Basförskjutning · steg", -10, 10, 0.1, "Utgångsläget som all kompensation läggs till på."],
  ["room_gain", "Rumspåverkan · steg/°C", 0, 5, 0.1, "Hur mycket en grads avvikelse påverkar förskjutningen."],
  ["deadband", "Tolerans runt målet · °C", 0, 2, 0.1, "Inom detta band ger rummen ingen extra kompensation."],
  ["max_step", "Maximal ändring per gång", 0.1, 2, 0.1, "Måste vara minst entitetens steglängd."],
  ["min_interval_minutes", "Minst mellan ändringar · minuter", 10, 1440, 1, "Räknas från senaste sparade styrförsök, även efter timeout. Omstart börjar inte om väntetiden."],
  ["max_age_minutes", "Högsta ålder på mätvärden · minuter", 5, 1440, 1, "För gamla eller saknade värden pausar automatiken."],
  ["auto_base_limit", "Automatisk baskorrigering högst · steg", 0.5, 5, 0.5, "Hur långt grundvärdet får flyttas automatiskt åt vardera hållet."],
  ["auto_base_memory_days", "Baskorrigeringens minne · dygn", 0, 30, 0.5, "0 = korrigeringen ligger kvar tills rummen säger annat. Annars bleknar den mot noll med denna tidskonstant, så att en missad värmekälla (en brasa, en solig vecka) inte gör huset för kallt i flera dygn efteråt."],
];
let settings;
let entities = [];
let entityAreas = {};
let areaError = "";
let busy = false;
let settingsRevision = 0;
let settingsBusy = false;
let discovery = null;
let discoveryError = "";
let modeDiscoveryRequest = 0;
let modeDiscoveryError = "";
let modeDiscoveryOffset = null;
let dashboardBusy = false;
let overviewBusy = false;
let historyBusy = false;
let planBusy = false;
let learningBusy = false;
let hotWaterBusy = false;
let energyBusy = false;
let auxiliaryBusy = false;
let savingsBusy = false;
let pageLayoutInitialized = false;
const pendingRefreshes = new Set();
const readRequests = new Map();
let entitiesBusy = false;
let entitiesReadAt = 0;
let eventRows = [];
let eventsError = "";
let lastStatus = null;
let summaryPlan = null;
let homeRoomSelection = "";
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
  const controller = new AbortController();
  const read = !options.method || options.method === "GET";
  if (read) readRequests.set(path, controller);
  const { timeoutMs = 20000, ...requestOptions } = options;
  const timeout = window.setTimeout(() => controller.abort(new Error("Hämtningen tog för lång tid. Försöker igen automatiskt.")), timeoutMs);
  try {
    const response = await fetch(`./api/${path}`, { cache: "no-store", ...requestOptions, signal: controller.signal });
    let body;
    try {
      body = await response.json();
    } catch (error) {
      // WebKit uses a DOMException named SyntaxError for invalid JSON.
      if (error.name !== "SyntaxError") throw error;
      const source = (response.headers.get("Content-Type") || "").includes("text/html")
        ? "Home Assistants ingress gav en webbsida i stället för API-data"
        : "API-svaret innehöll inte giltig JSON";
      const apiError = new Error(`${source} (api/${path}, HTTP ${response.status})`, { cause: error });
      apiError.status = response.status;
      throw apiError;
    }
    if (!response.ok) {
      const apiError = new Error(typeof body?.error === "string" ? body.error : `api/${path}: HTTP ${response.status}`);
      apiError.status = response.status;
      throw apiError;
    }
    return body;
  } finally {
    window.clearTimeout(timeout);
    if (readRequests.get(path) === controller) readRequests.delete(path);
  }
}

async function refreshEntities() {
  if (entitiesBusy || Date.now() - entitiesReadAt < 60000 || editingSettings()) return;
  entitiesBusy = true;
  try {
    const latest = await api("entities");
    entities = latest.sort((a, b) => a.name.localeCompare(b.name, "sv"));
    entitiesReadAt = Date.now();
    renderSettings(readSettings(), true);
    $("entities-status").textContent = "";
  } catch (error) {
    $("entities-status").textContent = `Enhetsvärden kunde inte uppdateras: ${error.message}. Försöker igen automatiskt.`;
  } finally {
    entitiesBusy = false;
  }
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

function selectOptions(id, predicate, configured = settings[id]) {
  const select = $(id);
  select.replaceChildren(new Option(id === "forecast_entity" ? "Automatiskt · SMHI i första hand"
    : id === "energy_entity" ? "Automatiskt · elmätare på vald myUplink-pump" : "Inte vald", ""));
  for (const entity of entities.filter(predicate)) {
    select.add(new Option(`${entity.name} · ${entity.entity_id}`, entity.entity_id));
  }
  if (configured && !Array.from(select.options).some((option) => option.value === configured)) {
    select.add(new Option(`${configured} (saknas / ej tillgänglig)`, configured));
  }
  select.value = configured;
}

// Pump settings chosen as a select entity plus two of its own options.
const MODE_PICKERS = {
  hot_water: {
    entity: "hot_water_mode_entity", options: ["hot_water_cheap_option", "hot_water_normal_option"],
    hint: /comfort_mode|hot_?water|varmvatten|dhw/i,
    // NIBE's usual pair when a newly chosen select has neither current mode.
    defaults: (options) => [options.includes("Normal") ? "Normal" : options[0], options.includes("Economy") ? "Economy" : options[1] ?? options[0]],
  },
  aux: {
    entity: "aux_limit_entity", options: ["aux_limit_option", "aux_normal_option"],
    hint: /electrical_add|eltillsats|add_heat/i,
    // Smallest step above zero when expensive, and whatever the pump uses today as the usual limit.
    defaults: (options, entity) => [options.find((o) => parseFloat(o) > 0) ?? options[0], entity?.state ?? options.at(-1)],
  },
};

function renderModeSelects(picker, entityId, first, second) {
  const { entity, hint } = MODE_PICKERS[picker];
  const select = $(entity);
  select.replaceChildren(new Option("Inte vald", ""));
  // Likely selects first; every select stays available for other pumps.
  const selects = entities.filter((e) => e.entity_id.startsWith("select."));
  const likely = (e) => hint.test(`${e.entity_id} ${e.name}`) && !/schedul|schema/i.test(e.entity_id);
  for (const item of [...selects.filter(likely), ...selects.filter((e) => !likely(e))]) {
    select.add(new Option(`${item.name} · ${item.entity_id}`, item.entity_id));
  }
  if (entityId && !selects.some((e) => e.entity_id === entityId)) select.add(new Option(`${entityId} (saknas / ej tillgänglig)`, entityId));
  select.value = entityId || "";
  renderModes(picker, first, second);
}

function pickerChanged(picker) {
  const { entity, options: ids, defaults } = MODE_PICKERS[picker];
  const chosen = entities.find((e) => e.entity_id === $(entity).value);
  const options = chosen?.options || [];
  const fallback = defaults(options, chosen);
  // A newly chosen select keeps the current modes if it has them.
  renderModes(picker, ...ids.map((id, i) => options.includes($(id).value) ? $(id).value : fallback[i]));
  renderModeDiscovery();
}

function renderModeDiscovery() {
  const found = discovery?.pump_modes;
  for (const [picker, prefix] of [["hot_water", "hot-water"], ["aux", "aux"]]) {
    const key = MODE_PICKERS[picker].entity;
    const selected = $(key).value;
    const matching = found?.offset_entity === $("offset_entity").value;
    const candidate = matching ? found?.[key] : "";
    const description = modeDiscoveryError || (!matching ? "Välj en pump för att söka efter entiteter."
      : found?.message || found?.[`${key}_message`] || "Automatisk upptäckt är inte tillgänglig.");
    $(`${prefix}-discovery`).textContent = modeDiscoveryError || (candidate
      ? `Förslag: ${candidate}. ${selected ? "Ditt nuvarande val ändras inte. Kontrollera att det tillhör vald pump." : "Använd förslaget och kontrollera lägena. Inget sparas eller aktiveras automatiskt."}`
      : `${description}${selected ? " Ditt nuvarande val ändras inte; kontrollera att det tillhör vald pump." : ""}`);
    $(`${prefix}-suggestion`).hidden = !!modeDiscoveryError || !candidate || !!selected;
  }
}

function useModeSuggestion(picker) {
  const key = MODE_PICKERS[picker].entity;
  const found = discovery?.pump_modes;
  if (!found || found.offset_entity !== $("offset_entity").value || $(key).value || !found[key]) return;
  $(key).value = found[key];
  if ($(key).value !== found[key]) {
    modeDiscoveryError = "Den föreslagna entiteten saknas i entitetslistan. Uppdatera eller välj manuellt.";
    renderModeDiscovery();
    return;
  }
  pickerChanged(picker);
  updateSaveState();
}

async function refreshPumpModes() {
  const request = ++modeDiscoveryRequest;
  const offset = $("offset_entity").value;
  modeDiscoveryOffset = offset;
  modeDiscoveryError = "";
  if (discovery) discovery.pump_modes = null;
  renderModeDiscovery();
  if (!offset) return;
  try {
    const result = await api(`discovery?offset_entity=${encodeURIComponent(offset)}`);
    if (request !== modeDiscoveryRequest || $("offset_entity").value !== offset) return;
    if (!discovery) discovery = {};
    discovery.pump_modes = result.pump_modes;
  } catch (error) {
    if (request !== modeDiscoveryRequest || $("offset_entity").value !== offset) return;
    modeDiscoveryError = `Kunde inte söka efter pumpens entiteter: ${error.message}. Välj manuellt.`;
  }
  renderModeDiscovery();
}

function renderModes(picker, first, second) {
  const { entity, options: ids, defaults } = MODE_PICKERS[picker];
  const chosen = entities.find((e) => e.entity_id === $(entity).value);
  const options = chosen?.options || [];
  const fallback = defaults(options, chosen);
  for (const [i, configured] of [first, second].entries()) {
    const select = $(ids[i]);
    select.replaceChildren(...options.map((option) => new Option(option, option)));
    if (configured && !options.includes(configured)) {
      select.add(new Option(options.length ? `${configured} (finns inte i pumpen)` : configured, configured));
    }
    select.value = configured || fallback[i] || "";
  }
}

let notifyServices = null;

function renderNotifyOptions(configured) {
  const select = $("notify_service");
  select.replaceChildren(new Option("Bara Home Assistants aviseringar", ""));
  for (const service of notifyServices || []) select.add(new Option(service.name, service.service));
  if (configured && !Array.from(select.options).some((option) => option.value === configured)) {
    select.add(new Option(`${configured}${notifyServices ? " (saknas i Home Assistant)" : ""}`, configured));
  }
  select.value = configured || "";
  // Without any phone or other notify service, explain how to get one instead of an empty list.
  $("notify-help").hidden = !notifyServices || notifyServices.length > 0;
  $("notify-test").disabled = !select.value;
}

async function refreshNotifyServices() {
  try {
    notifyServices = (await api("notify")).services;
    $("notify-status").textContent = "";
  } catch (error) {
    $("notify-status").textContent = `Notify-tjänsterna kunde inte hämtas: ${error.message}`;
  }
  renderNotifyOptions($("notify_service").value || settings.notify_service);
}

async function testNotify() {
  const service = $("notify_service").value;
  $("notify-test").disabled = true;
  $("notify-status").textContent = "Skickar…";
  try {
    await api("notify", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ service }) });
    $("notify-status").textContent = "Skickad. Kom den fram? Glöm inte att spara.";
  } catch (error) {
    $("notify-status").textContent = `Det gick inte: ${error.message}`;
  } finally {
    $("notify-test").disabled = !$("notify_service").value;
  }
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
    ? `Rapporterat värde: ${displayNumber(Number(offset.state))}. Intervall: ${displayNumber(Number(offset.min))} till ${displayNumber(Number(offset.max))}. Steglängd: ${displayNumber(Number(offset.step))}.${offset.unit ? ` Enhet: ${offset.unit}.` : ""}`
    : "Utan vald entitet kan du se temperaturförslag, men inte styra pumpen.";
  const price = entities.find((entity) => entity.entity_id === $("price_entity").value);
  $("price-unit").textContent = price
    ? `Senast hämtade givarvärde: ${price.state} ${price.unit}. Givaren avser priset just nu; den bekräftar inte att kommande priser finns. Enheten används för de fasta prisgränserna under Avancerat.`
    : "Välj givare för att se prisets enhet. Exempel: 1 kr/kWh = 100 öre/kWh.";
  renderPriceProvider();
}

function priceDiscovery(provider) {
  return discovery?.price_sources?.[provider]
    || (provider === "tibber" ? discovery : null);
}

function renderPriceProvider() {
  const provider = $("price_provider").value;
  const saved = provider === settings.price_provider && $("price_entity").value === settings.price_entity;
  const label = provider === "nordpool" ? "Nord Pool" : "Tibber";
  $("price-provider-status").textContent = `${saved ? "Sparad elpriskälla" : "Osparat val"}: ${label}. ${
    provider === "tibber"
      ? "Kommande priser hämtas från Tibber. Förbrukningsöversikten kan visa Tibbers husmätare."
      : "Kommande priser hämtas från vald Nord Pool-givare/integration. Enegics förbrukningsmätning är separat."
  }${saved ? "" : " Börjar gälla först när du sparar."}`;
  $("nordpool-help").hidden = provider !== "nordpool";
  $("price-discovery").textContent = priceDiscovery(provider)?.price_message || `Välj en prisgivare för ${label} manuellt.`;
}

function changePriceProvider() {
  const provider = $("price_provider").value;
  // Never carry a different provider's sensor across a source change.
  const selected = provider === settings.price_provider ? settings.price_entity : "";
  selectOptions("price_entity", (entity) => entity.entity_id.startsWith("sensor."),
    selected || priceDiscovery(provider)?.price_entity || "");
  $("manual-price").open = true;
  entityHints();
}

function renderPreheatExample() {
  const target = $("target_temperature").valueAsNumber;
  const degrees = $("preheat_degrees").valueAsNumber;
  if (!Number.isFinite(target) || !Number.isFinite(degrees)) {
    $("preheat-example").textContent = "Ange måltemperatur och tillfällig måländring för att se exemplet.";
    $("preheat-steps").replaceChildren();
    return;
  }
  const scope = $("priority_sensors").querySelector("input:checked") ? "prioriterade rum" : "valda rum";
  const draft = readSettings();
  const saved = settings && target === settings.target_temperature
    && degrees === settings.preheat_degrees && $("preheat_enabled").checked === settings.preheat_enabled
    && JSON.stringify(draft.priority_sensors) === JSON.stringify(settings.priority_sensors)
    && JSON.stringify(draft.room_sensors) === JSON.stringify(settings.room_sensors);
  $("preheat-example").textContent = `${saved ? "Dina inställningar" : "Exempel med osparade värden"}: vanligt mål ${displayNumber(target)} °C för ${scope} → före dyrperiod ${displayNumber(target + degrees)} °C → under dyrperiod ${displayNumber(target - degrees)} °C → tillbaka till ${displayNumber(target)} °C. ${$("preheat_enabled").checked ? "Planeringen är vald." : "Planeringen är av."}${saved ? "" : " Ändringarna börjar gälla först när du sparar."}`;
  $("preheat-steps").classList.toggle("off", !$("preheat_enabled").checked);
  $("preheat-steps").replaceChildren(...[
    ["Vanligt", target, ""], ["Före dyrt", target + degrees, "preheat"], ["Under dyrt", target - degrees, "coast"], ["Efter", target, ""],
  ].map(([label, value, kind]) => {
    const step = Object.assign(document.createElement("div"), { className: `preheat-step ${kind}` });
    step.append(Object.assign(document.createElement("small"), { textContent: label }),
      Object.assign(document.createElement("strong"), { textContent: `${displayNumber(value)}°` }));
    return step;
  }));
}

// Next 24 hours as a strip: red blocks = preheat, blue = allowed to cool, line = now.
function renderPreheatTimeline(windows, now) {
  const span = 24 * 3600;
  const strip = document.createElement("div");
  strip.className = "preheat-strip";
  for (const w of windows) {
    const from = Math.max(0, (w.start - now) / span), to = Math.min(1, (w.end - now) / span);
    if (to <= from) continue;
    const block = document.createElement("i");
    block.className = w.kind === "preheat" ? "preheat" : "coast";
    block.style.left = `${from * 100}%`;
    block.style.width = `${(to - from) * 100}%`;
    strip.append(block);
  }
  const ticks = document.createElement("div");
  ticks.className = "preheat-ticks";
  for (let i = 0; i <= 4; i++) {
    ticks.append(Object.assign(document.createElement("span"), {
      textContent: i === 0 ? "nu" : new Date((now + i * span / 4) * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" }),
    }));
  }
  $("preheat-timeline").replaceChildren(strip, ticks);
}

function renderSettings(formSettings = settings, preserveView = false) {
  for (const [key, value] of Object.entries(formSettings)) {
    const input = $(key);
    if (!input || ["room_sensors", "priority_sensors", "fireplace_rooms", "radiator_pairs"].includes(key)) continue;
    if (typeof value === "boolean") input.checked = value;
    else if (input.tagName !== "SELECT") input.value = value;
  }
  const isTemperature = (entity) => entity.entity_id.startsWith("sensor.")
    && (entity.device_class === "temperature" || ["°C", "°F"].includes(entity.unit));
  selectOptions("offset_entity", (entity) => entity.entity_id.startsWith("number."), formSettings.offset_entity);
  selectOptions("outdoor_entity", (entity) => isTemperature(entity) || entity.entity_id.startsWith("weather."), formSettings.outdoor_entity);
  selectOptions("price_entity", (entity) => entity.entity_id.startsWith("sensor."), formSettings.price_entity);
  selectOptions("forecast_entity", (entity) => entity.entity_id.startsWith("weather."), formSettings.forecast_entity);
  selectOptions("energy_entity", (entity) => entity.energy_meter_error === null, formSettings.energy_entity);
  selectOptions("house_power_entity", (entity) => entity.entity_id.startsWith("sensor.") && ["W", "kW"].includes(entity.unit), formSettings.house_power_entity);
  for (const id of ["grid_import_entity", "grid_export_entity", "solar_entity", "battery_discharge_entity", "battery_charge_entity"]) {
    selectOptions(id, (entity) => entity.entity_id.startsWith("sensor.") && ["kWh", "Wh", "MWh", "W", "kW"].includes(entity.unit), formSettings[id]);
  }
  renderNotifyOptions(formSettings.notify_service);
  renderModeSelects("hot_water", formSettings.hot_water_mode_entity, formSettings.hot_water_cheap_option, formSettings.hot_water_normal_option);
  renderModeSelects("aux", formSettings.aux_limit_entity, formSettings.aux_limit_option, formSettings.aux_normal_option);
  selectOptions("solar_power_entity", (entity) => entity.entity_id.startsWith("sensor.") && ["W", "kW"].includes(entity.unit), formSettings.solar_power_entity);
  for (const id of ["adaptive_reduction_mode", "solar_mode", "hot_water_habit_mode", "radiator_mode"]) $(id).value = formSettings[id];
  renderRadiatorPairs(formSettings.radiator_pairs, isTemperature);
  renderModeDiscovery();
  $("price_provider").value = formSettings.price_provider;
  $("price_mode").value = formSettings.price_mode;
  renderPriceMode();
  const rooms = entities.filter((entity) => isTemperature(entity) || entity.entity_id.startsWith("climate."));
  for (const id of formSettings.room_sensors) {
    if (!rooms.some((entity) => entity.entity_id === id)) rooms.push({ entity_id: id, name: id, state: "saknas", unit: "" });
  }
  $("room_sensors").replaceChildren();
  for (const entity of rooms) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    Object.assign(input, { type: "checkbox", value: entity.entity_id, checked: formSettings.room_sensors.includes(entity.entity_id) });
    const text = document.createElement("span");
    const climate = entity.entity_id.startsWith("climate.");
    label.className = "room-choice";
    label.dataset.climate = String(climate);
    const title = document.createElement("strong");
    title.textContent = entity.name;
    const measured = document.createElement("span");
    measured.className = "room-choice-temperature";
    const raw = Number(entity.state);
    measured.textContent = climate ? `${oneDecimal(entity.current_temperature)} °C`
      : `${entity.state !== "" && Number.isFinite(raw) ? oneDecimal(raw) : entity.state} ${entity.unit}`;
    const detail = document.createElement("small");
    const mode = { heat: "värme", auto: "automatik", off: "avstängd", cool: "kyla", heat_cool: "värme/kyla" }[entity.state] || entity.state;
    detail.textContent = climate
      ? `Termostat · ${entity.temperature_error ? "mätvärde saknas" : mode}${entity.setpoint != null ? ` · inställd ${displayNumber(entity.setpoint)} ${entity.temperature_unit || "°C"}` : ""}`
      : "Separat temperaturgivare";
    text.append(title, measured, detail);
    const id = document.createElement("small");
    id.textContent = entity.entity_id;
    const area = document.createElement("small");
    area.className = "room-choice-area";
    text.append(id, area);
    label.append(input, text);
    $("room_sensors").append(label);
  }
  renderPriorityPicker(formSettings.priority_sensors);
  renderNearestThermostat(formSettings.nearest_thermostat);
  renderFireplacePicker(formSettings.fireplace_rooms);
  filterRoomPicker();
  renderRoomPickerAreas();
  if (!preserveView) $("room-impact").open = true;
  if (!rooms.length) $("room_sensors").textContent = "Inga temperaturgivare hittades i Home Assistant.";
  $("mode").textContent = settings.enabled ? "Automatik aktiverad" : "Övervakning";
  $("mode").classList.toggle("active", settings.enabled);
  $("discovery-message").textContent = discoveryError || discovery?.message || "Automatisk upptäckt är inte tillgänglig.";
  if (!preserveView) {
    $("manual-pump").open = !formSettings.offset_entity;
    $("manual-outdoor").open = !formSettings.outdoor_entity;
    $("manual-price").open = !formSettings.price_entity;
    if (!formSettings.room_sensors.length || !formSettings.offset_entity) $("settings-panel").open = true;
  }
  targetChanged();
  renderIndoorDiff();
  renderSavedSettingsStatus();
  entityHints();
  if (discovery && modeDiscoveryOffset !== $("offset_entity").value) void refreshPumpModes();
}

async function findPump() {
  try {
    const query = settings.offset_entity ? `?offset_entity=${encodeURIComponent(settings.offset_entity)}` : "";
    discovery = await api(`discovery${query}`);
    modeDiscoveryOffset = discovery.pump_modes?.offset_entity || settings.offset_entity || discovery.offset_entity || "";
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
    const price = priceDiscovery(settings.price_provider || "tibber");
    if (!settings.price_entity && price?.price_entity) settings.price_entity = price.price_entity;
  } catch (error) {
    discoveryError = `Kunde inte upptäcka NIBE automatiskt: ${error.message}. Du kan välja anslutningen manuellt.`;
  }
}

function renderPriceMode() {
  const relative = $("price_mode").value === "relative";
  $("price-relative-fields").hidden = $("price-relative-help").hidden = !relative;
  for (const id of ["price_low", "price_high"]) $(id).closest("div").hidden = relative;
}

function renderStatusExtras(state) {
  const roomWarnings = new Set(state.room_sensor_warnings || []);
  const warnings = (state.warnings || []).filter((warning) => !roomWarnings.has(warning));
  $("status-warnings").replaceChildren(...warnings.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
  $("status-warnings").hidden = !warnings.length;
  const base = state.base_correction;
  $("auto-base-status").textContent = base?.enabled
    ? `Just nu ${base.value > 0 ? "+" : ""}${displayNumber(base.value)} steg av högst ±${displayNumber(base.limit)}.`
    : base && base.value ? `Avstängd. Sparad korrigering ${displayNumber(base.value)} steg används inte.` : "";
}

function readSettings() {
  const result = { ...settings };
  for (const [key, value] of Object.entries(settings)) {
    if (["room_sensors", "priority_sensors", "fireplace_rooms"].includes(key)) {
      // The pickers are sorted for display; keep the saved order so an unchanged selection
      // is not reported as an unsaved change. Newly chosen rooms are added last.
      const checked = Array.from($(key).querySelectorAll("input:checked"), (input) => input.value);
      result[key] = [...value.filter((id) => checked.includes(id)), ...checked.filter((id) => !value.includes(id))];
    } else if (key === "radiator_pairs") result[key] = readRadiatorPairs();
    else if (typeof value === "boolean") result[key] = $(key).checked;
    else if (typeof value === "number") result[key] = $(key).valueAsNumber;
    else result[key] = $(key).value;
  }
  return result;
}

// Radiator thermostat + room sensor pairs. Empty rows are ignored when saving.
function radiatorPairRow(pair, isTemperature) {
  const row = document.createElement("div");
  row.className = "radiator-pair field-pair";
  const make = (cls, label, predicate, value) => {
    const wrap = document.createElement("div");
    const caption = document.createElement("label");
    caption.textContent = label;
    const select = document.createElement("select");
    select.className = cls;
    select.add(new Option("Inte vald", ""));
    for (const entity of entities.filter(predicate)) select.add(new Option(`${entity.name} · ${entity.entity_id}`, entity.entity_id));
    if (value && !Array.from(select.options).some((option) => option.value === value)) select.add(new Option(`${value} (saknas)`, value));
    select.value = value || "";
    wrap.append(caption, select);
    return wrap;
  };
  const remove = document.createElement("button");
  Object.assign(remove, { type: "button", className: "secondary", textContent: "Ta bort" });
  remove.addEventListener("click", () => { row.remove(); updateSaveState(); });
  row.append(make("radiator-thermostat", "Termostat", (e) => e.entity_id.startsWith("climate."), pair.thermostat),
    make("radiator-sensor", "Rumsgivare i samma rum", isTemperature, pair.sensor), remove);
  return row;
}

let radiatorIsTemperature = () => false;
function renderRadiatorPairs(pairs, isTemperature) {
  radiatorIsTemperature = isTemperature;
  $("radiator_pairs").replaceChildren(...(pairs || []).map((pair) => radiatorPairRow(pair, isTemperature)));
}

function readRadiatorPairs() {
  return Array.from($("radiator_pairs").querySelectorAll(".radiator-pair"), (row) => ({
    thermostat: row.querySelector(".radiator-thermostat").value, sensor: row.querySelector(".radiator-sensor").value,
  })).filter((pair) => pair.thermostat || pair.sensor);
}

function renderRadiatorStatus() {
  const state = lastStatus?.radiators;
  const mode = settings?.radiator_mode;
  $("radiator-status").textContent = !mode || mode === "off" ? "Av. Inga radiatorbörvärden ändras."
    : !state ? "Väntar på nästa avläsning."
    : state.error ? state.error
    : `Läge ${RULE_MODES[mode]}. Stryper vid ${displayNumber(state.close_at)} °C, öppnar vid ${displayNumber(state.open_at)} °C.`;
  $("radiator-rows").replaceChildren(...(mode && mode !== "off" ? state?.rows || [] : []).map((row) => {
    const li = document.createElement("li");
    const name = entities.find((e) => e.entity_id === row.thermostat)?.name || row.thermostat;
    const room = row.room == null ? "" : ` · rum ${displayNumber(row.room)} °C`;
    const setpoint = row.setpoint == null ? "" : ` · börvärde ${displayNumber(row.setpoint)} °C`;
    li.textContent = `${name}${room}${setpoint}: ${[row.error, row.message].filter(Boolean).join(" · ")}`;
    return li;
  }));
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

function renderFireplacePicker(selected) {
  const ids = Array.from($("room_sensors").querySelectorAll("input:checked"), (input) => input.value);
  $("fireplace_rooms").replaceChildren(...ids.map((id) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    Object.assign(input, { type: "checkbox", value: id, checked: selected.includes(id), name: "fireplace-room" });
    const name = document.createElement("span");
    name.textContent = fireplaceRoomName(id);
    label.append(input, name);
    return label;
  }));
}

function fireplaceRoomName(id) {
  return [entityAreas[id]?.area_name, entities.find((e) => e.entity_id === id)?.name || id].filter(Boolean).join(" · ");
}

// Shared status pill and colour tone for the feature cards.
function featureTone(card, pill, tone, text) {
  $(card).dataset.tone = tone;
  $(pill).textContent = text;
}

let fireplaceManualBusy = false;

async function manualFireplace() {
  if (fireplaceManualBusy) return;
  fireplaceManualBusy = true;
  $("fireplace-manual-start").disabled = true;
  try {
    await api("fireplace", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "start" }) });
  } catch (error) {
    $("fireplace-manual-status").textContent = `Det gick inte: ${error.message}`;
    return;
  } finally {
    fireplaceManualBusy = false;
    $("fireplace-manual-start").disabled = false;
  }
  await refreshStatus();
}

const RULE_MODES = { off: "av", shadow: "skugga", on: "på" };

function renderRuleStatus() {
  const adaptive = lastStatus?.adaptive_reduction;
  const mode = settings?.adaptive_reduction_mode;
  $("adaptive-status").textContent = !mode || mode === "off" ? ""
    : !adaptive ? `Läge ${RULE_MODES[mode]}. Gör inget just nu: elpriset sänker inte värmen.`
    : adaptive.note ? `Läge ${RULE_MODES[mode]}. ${adaptive.note}`
    : `Läge ${RULE_MODES[mode]}. ${adaptive.applied ? "Sänker" : "Hade sänkt"} ${displayNumber(adaptive.reduction)} steg av högst ${displayNumber(adaptive.fixed)}: de två kallaste rummen (nu ${displayNumber(adaptive.start)} °C) förutsägs som lägst ${displayNumber(adaptive.predicted_min)} °C under ${displayNumber(adaptive.hours)} h dyr el, golv ${displayNumber(adaptive.floor)} °C.`;
  const solar = lastStatus?.solar;
  const solarMode = settings?.solar_mode;
  const sunNow = solar?.heating ? (solar.applied ? "Solen värmer huset: målet sänkt 0,3 °C." : "Solen värmer huset: målet hade sänkts 0,3 °C.")
    : "Solen värmer inte huset märkbart just nu.";
  const sunAhead = solar?.sunny_hours === null || solar?.sunny_hours === undefined ? "Ingen timprognos."
    : `${displayNumber(solar.sunny_hours)} soltimmar väntas närmaste 12 h${solar.sunny_ahead
      ? (solar.applied ? ", förvärmning och billig-el-höjning minskas." : ", förvärmning och billig-el-höjning hade minskats.") : "."}`;
  $("solar-status").textContent = !solarMode || solarMode === "off" || !solar ? ""
    : solar.note ? solar.note
    : `Läge ${RULE_MODES[solarMode]}. Solceller ${solar.power ?? "—"} W${solar.rising === null ? "" : `, rummen ${solar.rising >= 0 ? "+" : ""}${displayNumber(solar.rising)} °C/h`}. ${sunNow} ${sunAhead}`;
  const habit = lastStatus?.hot_water?.habit;
  const habitMode = settings?.hot_water_habit_mode;
  $("habit-status").textContent = !habitMode || habitMode === "off" || !habit ? ""
    : habit.note ? `Läge ${RULE_MODES[habitMode]}. ${habit.note}`
    : `Läge ${RULE_MODES[habitMode]}. Lärt av ${habit.days} dygn. ${habit.reason
      ? `Nu: ${habit.reason}${habit.would ? ` – hade valt ${habit.would}` : ""}.` : "Ingen trolig tappning de närmaste två timmarna."}`;
}

function renderHotWaterControl() {
  renderRuleStatus();
  const state = lastStatus?.hot_water;
  const text = !state?.enabled ? "" : state.error ? state.error
    : `Varmvattenläge ${state.current ?? "okänt"} · önskat ${state.desired} (${state.reason}).${state.note ? ` ${state.note}` : ""}${state.next_change_at
      ? ` Nästa ändring tidigast ${new Date(state.next_change_at * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}.` : ""}`;
  $("hw-control").textContent = text;
  $("hw-control").hidden = !text;
  $("hot-water-settings-status").textContent = text || (settings?.hot_water_price_enabled ? "Väntar på nästa avläsning." : "");
}

function renderAuxLimitControl() {
  const state = lastStatus?.aux_limit;
  const pending = lastStatus?.aux_recovery;
  const text = !state?.enabled ? (pending ? `Återställningen av elpatronens max eltillsats har inte kunnat bekräftas. Kontrollera ${pending.entity_id} på pumpen.` : "") : state.error ? state.error
    : `Max eltillsats ${state.current ?? "okänd"} · önskad ${state.desired} (${state.reason}).${state.hot_water && !state.hot_water.error
      ? ` Varmvatten: laddgivare ${displayNumber(state.hot_water.charge)} °C, topp ${displayNumber(state.hot_water.top)} °C.` : ""}${state.note ? ` ${state.note}` : ""}${state.next_change_at
      ? ` Nästa sänkning tidigast ${new Date(state.next_change_at * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}.` : ""}`;
  $("aux-limit-control").textContent = text;
  $("aux-limit-control").hidden = !text;
  $("aux-limit-settings-status").textContent = text || (settings?.aux_limit_enabled ? "Väntar på nästa avläsning." : "");
  $("aux-recovery-ack").hidden = !pending;
}

async function acknowledgeAuxRecovery() {
  const pending = lastStatus?.aux_recovery;
  if (!pending) return;
  if (!window.confirm(`Kontrollera direkt på pumpen att max eltillsats är ${pending.normal}. Fortsätt bara om du har kontrollerat värdet. Automatisk elpatronbegränsning stängs av.`)) return;
  $("aux-recovery-ack").disabled = true;
  try {
    const result = await api("aux-recovery", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmed: true, entity_id: pending.entity_id, normal: pending.normal }) });
    $("aux-recovery-status").textContent = result.message;
    await refreshStatus();
  } catch (error) {
    $("aux-recovery-status").textContent = `Kontrollen kunde inte sparas: ${error.message}`;
  } finally {
    $("aux-recovery-ack").disabled = false;
  }
}

function renderFireplace() {
  const state = lastStatus?.fireplace;
  $("fireplace-status").textContent = state?.message || (settings?.fireplace_enabled
    ? "Väntar på aktuell kaminobservation." : "Kaminobservation är av.");
  $("fireplace-control").textContent = state?.control_note || "";
  $("fireplace-control").hidden = !$("fireplace-control").textContent;
  $("fireplace-learning").textContent = state?.learned
    ? `Typisk observerad varaktighet: ${displayNumber(state.duration_hours)} timmar från ${state.completed} avslutade förlopp. Ingen säker prognos för den här eldningen.`
    : `Lär sig: ${state?.completed || 0} avslutade förlopp. Minst tre med samma rumsval behövs för typisk varaktighet och temperaturförändring.`;
  const completed = state?.completed || 0;
  $("fireplace-learn-count").textContent = state?.learned ? `Inlärd · ${completed} förlopp` : `${Math.min(completed, 3)} av 3`;
  $("fireplace-learn-bar").style.width = `${Math.round(Math.min(1, completed / 3) * 100)}%`;
  if (state?.active_since != null) $("fireplace-status").textContent += ` Observerat förlopp från ${new Date(state.active_since * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}.`;
  const enabled = state ? state.enabled : settings?.fireplace_enabled;
  const active = state?.phase === "active";
  // One press is enough; the observation decides when the extra heat has faded.
  $("fireplace-manual").hidden = !enabled || active;
  $("fireplace-manual-status").hidden = !enabled || active;
  if (!fireplaceManualBusy) $("fireplace-manual-status").textContent =
    "Starta kaminläget direkt i stället för att vänta på att extravärmen upptäcks. Det avslutas av sig självt när kaminrummet inte längre är varmare än övriga rum.";
  const [tone, text] = !enabled ? ["muted", "Av"]
    : state?.phase === "active" ? (state.contribution < 0 ? ["red", "Sänker värmen"] : ["amber", "Extravärme"])
    : state?.phase === "candidate" ? ["amber", "Misstänkt värme"]
    : state?.phase === "learning" ? ["blue", "Samlar data"]
    : state?.phase === "idle" ? ["green", "Bevakar"]
    : state?.phase === "unavailable" ? ["muted", "Saknar värden"] : ["muted", "Väntar"];
  featureTone("fireplace-card", "fireplace-pill", tone, text);
  $("fireplace-effects").replaceChildren(...(state?.room_effects || []).map((room) => {
    const item = document.createElement("li");
    const name = entityAreas[room.entity_id]?.area_name || entities.find((e) => e.entity_id === room.entity_id)?.name || room.entity_id;
    const strong = Object.assign(document.createElement("strong"), { textContent: `+${displayNumber(room.rise)} °C` });
    item.append(strong, `${name}: typisk uppmätt ökning +${displayNumber(room.rise)} °C${room.arrival_minutes == null ? "" : ` · först observerad +0,3 °C efter cirka ${room.arrival_minutes} min`}. Inte säkert orsakat av kaminen.`);
    return item;
  }));
  const recent = (state?.recent || []).slice().reverse();
  $("fireplace-history").replaceChildren(...(recent.length ? recent.map((episode) => {
    const item = document.createElement("li");
    item.className = episode.complete ? "complete" : "uncertain";
    const when = Object.assign(document.createElement("strong"), {
      textContent: `${new Date(episode.start * 1000).toLocaleString("sv-SE", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} – ${new Date(episode.end * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}`,
    });
    const what = Object.assign(document.createElement("span"), {
      textContent: episode.complete ? "lokal extravärme klingade av" : "avbruten/osäker observation, används inte för typisk varaktighet",
    });
    item.append(when, what);
    return item;
  }) : [Object.assign(document.createElement("li"), { className: "empty", textContent: "Inga misstänkta perioder ännu." })]));
}

let fireplaceSavingsBusy = false;

async function refreshFireplaceSavings() {
  if (fireplaceSavingsBusy) return;
  fireplaceSavingsBusy = true;
  try {
    const data = await api("fireplace-savings?days=30", { timeoutMs: 60000 });
    const currency = data.currency || "kr";
    const model = data.model;
    const rows = data.episodes || [];
    const t = data.totals || {};
    $("fireplace-savings-metrics").replaceChildren(...(t.fires ? [
      metric({ label: `Sparad el · ${t.fires} ${t.fires === 1 ? "brasa" : "brasor"}`, value: displayNumber(Math.round(t.saved_kwh)),
        unit: "kWh", detail: `ungefär ±${displayNumber(Math.round(t.margin_kwh))} kWh` }),
      metric({ label: t.cost_complete ? "Sparat i pengar" : "Sparat i pengar · timmar med pris", value: t.saved_cost == null ? null : displayNumber(Math.round(t.saved_cost)),
        unit: currency, detail: "pumpens el, inte veden" }),
      metric({ label: "Per eldad timme", value: t.fire_hours ? displayNumber(Math.round(t.saved_kwh / t.fire_hours * 10) / 10) : null,
        unit: "kWh", detail: `${displayNumber(Math.round(t.fire_hours))} timmar eldning` }),
    ] : []));
    $("fireplace-savings-list").replaceChildren(...rows.slice().reverse().map((row) => {
      const item = document.createElement("li");
      item.className = row.clear ? "complete" : "uncertain";
      const when = Object.assign(document.createElement("strong"), {
        textContent: `${new Date(row.start * 1000).toLocaleString("sv-SE", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} · ${displayNumber(Math.round(row.fire_hours * 10) / 10)} h`,
      });
      const text = row.saved_kwh == null ? "underlag saknas för den tiden (pumpens el eller utetemperatur)"
        : `${row.saved_kwh >= 0 ? "sparade" : "använde"} cirka ${displayNumber(Math.round(Math.abs(row.saved_kwh) * 10) / 10)} kWh${row.saved_kwh < 0 ? " mer" : ""}${row.saved_cost == null ? "" : ` (${displayNumber(Math.round(Math.abs(row.saved_cost)))} ${currency})`}`
          + ` · ${row.clear ? "tydlig skillnad" : "inom osäkerheten"} (±${displayNumber(Math.round(row.margin_kwh))} kWh)`
          + `${row.complete ? "" : ` · el för ${row.covered_hours} av ${row.window_hours} timmar`}`
          + `${row.mean_outdoor == null ? "" : ` · ute ${oneDecimal(row.mean_outdoor)} °C`}`;
      item.append(when, Object.assign(document.createElement("span"), { textContent: text }));
      return item;
    }));
    $("fireplace-savings-status").textContent = !model
      ? (data.has_outdoor ? "För få timmar utan brasa med pumpens el och utetemperatur för att räkna ännu (minst 72 behövs)."
        : "Utetemperaturens historik saknas än. Den samlas av husets inlärning; titta igen om några timmar.")
      : `Jämfört med ${displayNumber(model.hours)} timmar utan brasa (ute ${oneDecimal(model.outdoor_min)} till ${oneDecimal(model.outdoor_max)} °C), där pumpen använde i snitt ${oneDecimal(model.intercept + model.slope * ((model.outdoor_min + model.outdoor_max) / 2))} kWh i timmen.`
        + `${rows.length ? "" : " Inga avslutade brasor i perioden."}${data.ongoing ? " En pågående brasa räknas när den och de sex timmarna efter är slut." : ""}`
        + `${data.method === "estimate" ? " Pumpens el är uppskattad, eftersom pumpen saknar elmätare." : ""}`;
  } catch (error) {
    $("fireplace-savings-metrics").replaceChildren();
    $("fireplace-savings-list").replaceChildren();
    $("fireplace-savings-status").textContent = error.message;
  } finally {
    fireplaceSavingsBusy = false;
  }
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

// "Givare kök Temperatur" -> "Givare kök": the trailing measurement word repeats what the card shows.
function roomLabel(name) {
  const short = String(name || "").replace(/\s+(temperatur|temperature|temp)$/i, "").trim();
  return short || String(name || "");
}

function roomTrend(room) {
  if (room.error) return room.error;
  const line = historyData?.series.find((line) => line.entity_id === room.entity_id);
  const stamp = (lastStatus?.checked_at || Date.now() / 1000) - 3600;
  const previous = line?.points.findLast((point) => point[0] <= stamp)?.[1];
  if (room.temperature == null || typeof previous !== "number") return "Timförändring saknas";
  const delta = Math.round((room.temperature - previous) * 100) / 100;
  return `${delta > 0 ? "+" : ""}${oneDecimal(delta)} °C senaste timmen`;
}

function renderRoomImpact() {
  $("indoor-label").textContent = settings?.priority_sensors.length ? "Prioriterade rum nu" : "Inne nu";
  $("house-indoor").textContent = oneDecimal(lastStatus?.house_indoor);
  const rooms = lastStatus?.rooms || [];
  renderHomeHistory();
  const groups = groupRooms(rooms, (room) => room.entity_id);
  $("room-area-status").textContent = areaError
    ? `Home Assistant-rummens namn kunde inte läsas: ${areaError}. Visar givarna utan rumsgruppering.`
    : "Rumsnamnen hämtas från områdena i Home Assistant.";
  replaceIfChanged($("room-impact-list"), [...groups.values()].map(({ name, rooms: members }) => {
    const group = document.createElement("section");
    group.className = "room-area-group";
    const heading = document.createElement("h3");
    heading.textContent = name;
    const cards = document.createElement("div");
    cards.className = "home-room-grid";
    for (const room of members) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "home-room-card";
      card.classList.toggle("selected", room.entity_id === homeRoomSelection);
      const tooWarm = lastStatus.warm_rooms?.includes(room.entity_id) || false;
      card.classList.toggle("too-warm", tooWarm);
      card.setAttribute("aria-pressed", String(room.entity_id === homeRoomSelection));
      card.setAttribute("aria-label", `${room.name}, ${displayNumber(room.temperature)} °C${room.setpoint != null ? `, termostat ${displayNumber(room.setpoint)} °C` : ""}${tooWarm ? ", över varmgränsen" : ""}. Visa historik för rummet.`);
      card.addEventListener("click", () => {
        homeRoomSelection = room.entity_id;
        renderHomeHistory();
        renderRoomImpact();
      });
      const header = document.createElement("span");
      header.className = "home-room-card-header";
      const title = document.createElement("strong");
      title.textContent = roomLabel(room.name);
      title.title = room.name;
      header.append(title);
      if (tooWarm) {
        const warning = document.createElement("span");
        warning.className = "home-room-warning";
        warning.textContent = "Över gränsen";
        header.append(warning);
      }
      const reading = document.createElement("span");
      reading.className = "home-room-reading";
      const value = document.createElement("span");
      value.className = "home-room-temperature";
      value.textContent = oneDecimal(room.temperature);
      const unit = document.createElement("span");
      unit.className = "home-room-unit";
      unit.textContent = "°C";
      value.append(unit);
      const difference = document.createElement("span");
      difference.className = "home-room-difference";
      const diff = room.temperature == null || !settings ? null : room.temperature - settings.target_temperature;
      difference.textContent = diff == null ? room.error || "Mätvärde saknas"
        : Math.abs(diff) < 0.1 ? "Vid ditt mål" : `${oneDecimal(Math.abs(diff))}° ${diff > 0 ? "över" : "under"} ditt mål`;
      reading.append(value, difference);
      const thermostat = document.createElement("span");
      thermostat.className = "home-room-thermostat";
      const thermostatLabel = document.createElement("span");
      thermostatLabel.className = "home-room-thermostat-label";
      thermostatLabel.textContent = room.setpoint != null ? "Termostatens börvärde" : "Enhet";
      const setpoint = document.createElement("strong");
      setpoint.textContent = room.setpoint != null ? `${displayNumber(room.setpoint)} °C` : "Temperaturgivare";
      thermostat.append(thermostatLabel, setpoint);
      const control = lastStatus?.thermostat_control?.rooms.find((entry) => entry.entity_id === room.entity_id);
      if (room.setpoint == null && room.thermostat_error) setpoint.textContent = room.thermostat_error;
      const footer = document.createElement("span");
      footer.className = "home-room-card-footer";
      const role = document.createElement("span");
      role.className = "home-room-role";
      role.textContent = !Array.isArray(lastStatus?.indoor_rooms) || lastStatus?.error_message
        ? "Styrande snitt kan inte bedömas"
        : lastStatus.indoor_rooms.includes(room.entity_id) ? "Ingår i styrande snitt"
          : room.error ? "Ingår inte i styrande snitt · mätvärde saknas"
            : "Ingår inte i styrande snitt";
      if (typeof lastStatus?.house_indoor === "number") role.textContent += " · ingår i husets snitt";
      const trend = document.createElement("small");
      trend.textContent = roomTrend(room);
      const tags = [room.priority ? "Prioriterat" : "", room.nearest ? "Närmast pumpen" : "",
        settings?.fireplace_rooms.includes(room.entity_id) ? "Kaminrum" : ""].filter(Boolean);
      const note = document.createElement("small");
      note.textContent = [tags.join(" · "), control?.error || control?.message || "", control?.warning || ""].filter(Boolean).join(" · ");
      footer.append(role, trend);
      if (note.textContent) footer.append(note);
      if (room.entity_id.startsWith("climate.")) {
        const sensorNote = document.createElement("small");
        sensorNote.className = "home-room-sensor-note";
        sensorNote.textContent = "Termostatens givare kan påverkas av värmen från ett närliggande element.";
        footer.append(sensorNote);
      }
      // A plain temperature sensor has no setpoint: skip the row instead of saying "Enhet: Temperaturgivare".
      card.append(header, reading, ...(room.setpoint != null || room.thermostat_error ? [thermostat] : []), footer);
      cards.append(card);
    }
    group.append(heading, cards);
    return group;
  }));
  renderComfort();
  const control = lastStatus?.thermostat_control;
  $("thermostat-control-status").textContent = lastStatus?.error_message
    ? `Pausad: ${lastStatus.error_message}`
    : control?.enabled ? `Termostatbegränsning på${lastStatus.enabled ? "" : " · huvudautomatiken är av, inga nya ändringar skickas"}.`
      : "Termostatbegränsning av. Inga börvärden ändras.";
  renderRadiatorStatus();
  $("thermostat-control-rooms").replaceChildren(...(control?.rooms || []).map((room) => {
    const li = document.createElement("li");
    li.textContent = `${entities.find((e) => e.entity_id === room.entity_id)?.name || room.entity_id}: ${room.error || room.message} ${room.warning || ""}`;
    return li;
  }));
}

function renderHomeHistory() {
  const select = $("home-history-entity");
  if (!select) return;
  const lines = (historyData?.series || []).filter((line) => !line.outdoor);
  if (!lines.some((line) => line.entity_id === homeRoomSelection)) {
    homeRoomSelection = settings?.priority_sensors?.find((id) => lines.some((line) => line.entity_id === id))
      || lines[0]?.entity_id || "";
  }
  fillSelect(select, lines.map((line) => new Option(roomLabel(line.name), line.entity_id)), homeRoomSelection);
  select.disabled = lines.length === 0;
  const selected = lines.find((line) => line.entity_id === homeRoomSelection);
  const indoorSeries = selected ? [
    { name: `${roomLabel(selected.name)} · uppmätt`, points: selected.points, until: historyData.end, color: 0 },
    ...(selected.kind === "climate" ? [{
      name: `${roomLabel(selected.name)} · termostatinställning`, points: selected.setpoints || [], until: historyData.end, color: 0, dashed: true,
    }] : []),
  ] : [];
  drawChart($("home-room-chart"), indoorSeries, {
    start: historyData?.start || Date.now() / 1000 - 86400,
    end: historyData?.end || Date.now() / 1000,
    height: 190,
    guides: selected?.kind === "climate" ? [] : historyData ? [{ value: historyData.target, label: `mål ${displayNumber(historyData.target)}°` }] : [],
    empty: historyData ? "Ingen sparad temperaturhistorik för det här rummet." : "Temperaturhistorik saknas.",
    label: selected ? `${selected.name}: temperatur senaste dygnet` : "Rumstemperatur senaste dygnet",
  });
  legend($("home-room-legend"), indoorSeries);
  $("home-room-history-status").textContent = selected
    ? `${roomLabel(selected.name)} · uppmätt temperatur senaste 24 timmarna${selected.kind === "climate" ? " · börvärde visas med streckad linje" : ""}`
    : "Välj en temperaturgivare i inställningarna för att visa rumshistorik.";

  const now = summaryPlan?.now || historyData?.end || Date.now() / 1000;
  const outdoorHistory = summaryPlan?.outdoor?.history
    || historyData?.series.find((line) => line.outdoor)?.points || [];
  const outdoorForecast = summaryPlan?.outdoor?.forecast || [];
  const outdoorSeries = [
    { name: "Uppmätt ute", points: outdoorHistory, until: now, color: 2 },
    { name: "Prognos ute", points: outdoorForecast, step: false, dashed: true, color: 2 },
  ];
  drawChart($("home-weather-chart"), outdoorSeries, {
    start: historyData?.start || now - 86400,
    end: Math.max(now + 86400, summaryPlan?.end || now + 86400),
    height: 190,
    now,
    empty: summaryPlan?.outdoor?.error || "Ingen utetemperaturhistorik eller prognos att visa.",
    label: "Uppmätt utetemperatur och väderprognos",
  });
  legend($("home-weather-legend"), outdoorSeries);
  $("home-weather-status").textContent = outdoorForecast.some((point) => point[1] !== null)
    ? "Heldragen linje = uppmätt · streckad linje = prognos, inte en uppmätt temperatur."
    : summaryPlan?.outdoor?.error || "Prognos saknas. Visar bara utetemperatur som Home Assistant har sparat.";
}

function groupRooms(rooms, entityId) {
  const groups = new Map();
  for (const room of rooms) {
    const area = entityAreas[entityId(room)] || {};
    const key = area.area_id || area.area_name || "__unassigned__";
    if (!groups.has(key)) groups.set(key, { name: area.area_name || "Inget område i Home Assistant", rooms: [] });
    groups.get(key).rooms.push(room);
  }
  return groups;
}

function renderRoomPickerAreas() {
  for (const label of $("room_sensors").querySelectorAll(".room-choice")) {
    const id = label.querySelector("input").value;
    const area = label.querySelector(".room-choice-area");
    area.textContent = areaError ? "Home Assistant-rum kunde inte läsas"
      : entityAreas[id]?.area_name ? `Home Assistant-rum: ${entityAreas[id].area_name}`
        : "Inget område i Home Assistant";
  }
  for (const label of $("fireplace_rooms").querySelectorAll("label")) {
    label.querySelector("span").textContent = fireplaceRoomName(label.querySelector("input").value);
  }
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
  const missingRoom = controlling.some((id) => !rooms.some((room) => room.entity_id === id && room.temperature != null));
  const [tone, pill] = !rooms.length ? ["muted", "Inga värden"] : cold.length ? ["amber", cold.length === 1 ? "1 rum under målet" : `${cold.length} rum under målet`]
    : missingRoom ? ["red", "Givare saknas"] : ["green", "Styrande rum inte kalla"];
  featureTone("comfort-card", "comfort-pill", tone, pill);
  $("comfort-advice").replaceChildren(...items.map((text, index) => {
    const li = document.createElement("li");
    const room = cold[index];
    if (!room) { li.className = "room-advice-note"; li.textContent = text; return li; }
    li.className = "room-advice";
    const head = document.createElement("div");
    head.className = "room-advice-head";
    head.append(Object.assign(document.createElement("strong"), { textContent: room.name }),
      Object.assign(document.createElement("span"), { textContent: `${displayNumber(room.temperature)} °C av ${displayNumber(settings.target_temperature)} °C` }));
    // The bar spans target − 3 °C … target, so a room 1 °C short is two thirds full.
    const track = Object.assign(document.createElement("div"), { className: "feature-track" });
    const fill = document.createElement("i");
    fill.style.width = `${Math.round(Math.max(0.04, Math.min(1, 1 - (settings.target_temperature - room.temperature) / 3)) * 100)}%`;
    track.append(fill);
    li.append(head, track, Object.assign(document.createElement("p"), { textContent: text.replace(`${room.name}: ${displayNumber(room.temperature)} °C, mål ${displayNumber(settings.target_temperature)} °C. `, "") }));
    return li;
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

// Temperatures and offsets in running text: one decimal is what the sensors can really tell apart.
function oneDecimal(value) {
  return typeof value === "number" ? value.toLocaleString("sv-SE", { maximumFractionDigits: 1 }) : "—";
}

// Axis steps of 1, 2, 2.5 or 5 times a power of ten, so labels read 20°, 22°, 24° instead of 21,75°.
function niceStep(span, count) {
  const raw = span / count, power = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((factor) => factor * power).find((step) => step >= raw) ?? 10 * power;
}

const dayAndTime = (time) => new Date(time * 1000).toLocaleString("sv-SE", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
// "18 nov. 20:00 – 19 nov. 20:00", or "19 nov. 19:32–20:07" within one day.
const periodText = (start, end) => new Date(start * 1000).toDateString() === new Date(end * 1000).toDateString()
  ? `${dayAndTime(start)}–${clock(end)}` : `${dayAndTime(start)} – ${dayAndTime(end)}`;
// Short myUplink outages are filled with the last known value; say so instead of hiding it.
const bridgedText = (seconds) => seconds >= 60
  ? ` ${displayNumber(Math.round(seconds / 60))} min med korta avbrott i myUplink har fyllts med senast kända värde.` : "";

function duration(seconds) {
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h${minutes % 60 ? ` ${minutes % 60} min` : ""}`;
}

let statusBusy = false;

const vacationTime = (time) => new Date(time * 1000).toLocaleString("sv-SE", { dateStyle: "short", timeStyle: "short" });
const vacationDay = (time) => new Date(time * 1000).toLocaleString("sv-SE", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
let vacationBusy = false;
let vacationDraft = false;
let vacationRendered;

// Day and time as two dropdowns that behave like a datetime-local input (value "YYYY-MM-DDTHH:MM", "" = empty).
// The native picker follows the phone's locale and is unreliable inside the Home Assistant app.
function whenPicker(box) {
  const pad = (number) => String(number).padStart(2, "0");
  const isoDay = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const days = document.createElement("select"), times = document.createElement("select");
  days.setAttribute("aria-label", `${box.dataset.label}, dag`);
  times.setAttribute("aria-label", `${box.dataset.label}, klockslag`);
  let disabled = false;
  const fillDays = (keep) => {
    const today = new Date(), first = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    days.replaceChildren(new Option(box.dataset.empty, ""));
    for (let index = 0; index < 366; index++) {
      const day = new Date(first);
      day.setDate(first.getDate() + index);
      const label = day.toLocaleDateString("sv-SE", { weekday: "short", day: "numeric", month: "short",
        year: day.getFullYear() === today.getFullYear() ? undefined : "numeric" });
      days.append(new Option(index === 0 ? `i dag (${label})` : index === 1 ? `i morgon (${label})` : label, isoDay(day)));
    }
    if (keep && ![...days.options].some((option) => option.value === keep)) days.append(new Option(keep, keep));
  };
  const fillTimes = (keep) => {
    times.replaceChildren();
    for (let minutes = 0; minutes < 1440; minutes += 15) {
      const text = `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`;
      times.append(new Option(text, text));
    }
    if (keep && ![...times.options].some((option) => option.value === keep)) {
      times.append(new Option(keep, keep));
      [...times.options].sort((a, b) => a.value.localeCompare(b.value)).forEach((option) => times.append(option));
    }
  };
  const sync = () => { days.disabled = disabled; times.disabled = disabled || !days.value; };
  fillDays(); fillTimes();
  times.value = "12:00";
  days.addEventListener("change", sync);
  Object.defineProperty(box, "value", {
    get: () => (days.value ? `${days.value}T${times.value}` : ""),
    set: (text) => {
      const [day = "", time = ""] = String(text || "").split("T");
      fillDays(day);
      fillTimes(time.slice(0, 5));
      days.value = day;
      times.value = time.slice(0, 5) || "12:00";
      sync();
    },
  });
  Object.defineProperty(box, "disabled", { get: () => disabled, set: (value) => { disabled = !!value; sync(); } });
  box.replaceChildren(days, times);
  sync();
}

function vacationInputTime(stamp) {
  const date = new Date(stamp * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function vacationTimestamp(value) {
  const stamp = new Date(value).getTime() / 1000;
  if (!Number.isFinite(stamp) || vacationInputTime(stamp) !== value
      || [-7200, -3600, -1800, 1800, 3600, 7200].some((delta) => vacationInputTime(stamp + delta) === value)) return NaN;
  return stamp;
}

function renderVacation() {
  const plan = lastStatus?.vacation || null;
  const phase = plan?.phase;
  $("vacation-status").textContent = !lastStatus ? "Semesterläget kan inte läsas just nu."
    : !plan ? "Ingen semester planerad."
    : phase === "scheduled" ? `Planerad ${vacationTime(plan.start)} till ${vacationTime(plan.end)}.`
    : phase === "restoring" ? "Semestern är slut. Återställer pumpens läge; automatiken väntar på återläsning."
    : plan.confirmed ? `Semesterläge på till ${vacationTime(plan.end)}. Automatiken väntar.`
    : `Sätter pumpen i semesterläge, till ${vacationTime(plan.end)}. Automatiken fortsätter tills pumpen bekräftat.`;
  $("vacation-error").textContent = plan?.error || "";
  $("vacation-error").hidden = !plan?.error;
  $("vacation-save").textContent = phase === "active" ? "Ändra sluttid" : "Spara semester";
  $("vacation-save").disabled = vacationBusy || phase === "restoring" || !lastStatus;
  $("vacation-cancel").hidden = !plan || phase === "restoring";
  $("vacation-cancel").disabled = vacationBusy;
  $("vacation-start").disabled = phase === "active" || phase === "restoring";
  const state = !lastStatus ? "unknown" : !plan ? "none" : phase === "active" && !plan.confirmed ? "starting" : phase;
  $("vacation-card").dataset.phase = state;
  $("vacation-pill").textContent = { unknown: "Okänt", none: "Ingen planerad", scheduled: "Planerad",
    starting: "Startar", active: "Pågår", restoring: "Återställer" }[state] || "Okänt";
  $("vacation-timeline").hidden = !plan;
  if (plan) {
    const now = Date.now() / 1000;
    $("vacation-from-label").textContent = vacationDay(plan.start);
    $("vacation-to-label").textContent = vacationDay(plan.end);
    $("vacation-length").textContent = phase === "active" && now > plan.start
      ? `${vacationLength(plan.end - now)} kvar` : vacationLength(plan.end - plan.start);
    const done = plan.end > plan.start ? (now - plan.start) / (plan.end - plan.start) : 1;
    $("vacation-progress").style.width = `${Math.round(Math.min(1, Math.max(0, done)) * 100)}%`;
  }
  $("vacation-presets").hidden = phase === "restoring";
  $("vacation-start-hint").textContent = phase === "active" || phase === "restoring" ? "Semestern har börjat" : "Tomt betyder nu";
  if (lastStatus && !vacationDraft && JSON.stringify(plan) !== vacationRendered) {
    $("vacation-start").value = plan ? vacationInputTime(plan.start) : "";
    $("vacation-end").value = plan ? vacationInputTime(plan.end) : "";
    vacationRendered = JSON.stringify(plan);
  }
  renderVacationDuration();
}

function vacationLength(seconds) {
  const hours = Math.max(0, Math.round(seconds / 3600));
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  if (!days) return `${rest} h`;
  return `${days} dygn${rest ? ` ${rest} h` : ""}`;
}

function vacationDraftStart() {
  const plan = lastStatus?.vacation;
  if (plan?.phase === "active") return plan.start;
  const value = $("vacation-start").value;
  const stamp = value ? vacationTimestamp(value) : Date.now() / 1000;
  return Number.isFinite(stamp) ? stamp : null;
}

function renderVacationDuration() {
  const end = $("vacation-end").value ? vacationTimestamp($("vacation-end").value) : null;
  const start = vacationDraftStart();
  $("vacation-duration").textContent = end === null || !Number.isFinite(end) ? "Välj hemkomst"
    : start === null || end <= start ? "Hemkomsten måste vara efter avresan"
    : `Borta ${vacationLength(end - start)}`;
}

function vacationPreset(days) {
  const start = vacationDraftStart() ?? Date.now() / 1000;
  const end = new Date((start + days * 86400) * 1000);
  end.setMinutes(0, 0, 0);
  if (end.getTime() / 1000 <= start) end.setHours(end.getHours() + 1);
  $("vacation-end").value = vacationInputTime(end.getTime() / 1000);
  vacationDraft = true;
  renderVacationDuration();
}

async function vacationAction(body) {
  if (vacationBusy) return;
  vacationBusy = true;
  renderVacation();
  try {
    const { vacation } = await api("vacation", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (lastStatus) lastStatus.vacation = vacation;
    vacationDraft = false;
    vacationRendered = undefined;
    renderVacation();
    void refreshDashboard();
  } catch (error) {
    $("vacation-error").textContent = error.message;
    $("vacation-error").hidden = false;
  } finally {
    vacationBusy = false;
    $("vacation-save").disabled = !lastStatus || lastStatus.vacation?.phase === "restoring";
    $("vacation-cancel").disabled = false;
  }
}

function saveVacation() {
  const seconds = (id) => $(id).value ? vacationTimestamp($(id).value) : null;
  const end = seconds("vacation-end");
  if (end === null || !Number.isFinite(end)) {
    $("vacation-error").textContent = end === null ? "Ange när du kommer hem." : "Välj en giltig och entydig hemkomsttid; tiden kan saknas eller förekomma två gånger vid tidsomställning.";
    $("vacation-error").hidden = false;
    return;
  }
  const start = lastStatus?.vacation?.phase === "active" ? null : seconds("vacation-start");
  if (start !== null && !Number.isFinite(start)) {
    $("vacation-error").textContent = "Välj en giltig och entydig starttid; tiden kan saknas eller förekomma två gånger vid tidsomställning.";
    $("vacation-error").hidden = false;
    return;
  }
  if (!lastStatus?.vacation && !window.confirm("Pumpens eget semesterläge aktiveras under perioden. Kontrollera pumpens inställningar för värme, varmvatten och ventilation. Spara perioden?")) return;
  void vacationAction({ action: "schedule", start, end });
}

async function refreshStatus() {
  if (statusBusy) { pendingRefreshes.add("status"); return; }
  statusBusy = true;
  try {
    void refreshSavedSettings();
    void refreshEntities();
    const response = await api("status");
    const stale = !response.checked_at || Date.now() / 1000 - response.checked_at > 180;
    const state = stale ? {
      ...response, indoor: null, outdoor: null, house_indoor: null, current_offset: null,
      desired_offset: null, next_offset: null, contributions: null, warm_rooms: [], thermostat_control: null,
      fireplace: null,
      indoor_rooms: null,
      rooms: (response.rooms || []).map((room) => ({ ...room, temperature: null, setpoint: null,
        error: "Avläsningen är inte aktuell.", thermostat_error: "Avläsningen är inte aktuell." })),
    } : response;
    if (typeof state.enabled === "boolean") {
      const away = state.vacation?.phase === "restoring" || (state.vacation?.phase === "active" && state.vacation.confirmed);
      $("mode").textContent = away ? "Semesterläge" : state.outage_since != null ? "Väntar på pumpen" : state.enabled ? "Automatik aktiverad" : "Övervakning";
      $("mode").classList.toggle("active", state.enabled && !away && state.outage_since == null);
    }
    lastStatus = state;
    renderVacation();
    renderFireplace();
    renderHotWaterControl();
    renderAuxLimitControl();
    renderRoomImpact();
    $("indoor").textContent = displayNumber(state.indoor);
    renderIndoorDiff();
    $("current-offset").textContent = displayNumber(state.current_offset);
    $("hero-offset").textContent = displayNumber(state.current_offset);
    const correction = state.base_correction?.enabled && state.base_correction.value ? state.base_correction.value : 0;
    $("hero-offset-base").textContent = typeof settings?.base_offset === "number"
      ? ` · grundvärde ${displayNumber(settings.base_offset)}${correction ? ` ${correction > 0 ? "+" : "−"} ${oneDecimal(Math.abs(correction))} auto` : ""}` : "";
    renderStatusExtras(state);
    $("outdoor-now").textContent = displayNumber(state.outdoor);
    $("outdoor-now").title = state.reading_errors?.outdoor || "";
    $("indoor").title = state.reading_errors?.indoor || "";
    $("hero-offset").title = state.reading_errors?.current_offset || "";
    $("desired-offset").textContent = displayNumber(state.desired_offset);
    $("next-offset").textContent = displayNumber(state.next_offset);
    $("status-title").textContent = state.error_message ? "Styrningen är pausad" : state.outage_since != null ? "Väntar på pumpens värden" : state.enabled ? "Värmen följer dina inställningar" : "Se förslaget innan du styr";
    $("status-message").textContent = state.error_message || state.message;
    if (summaryPlan) renderSummary(summaryPlan);
    else renderLiveAdjustment();
    const checked = state.checked_at ? new Date(state.checked_at * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" }) : "väntar";
    $("updated").textContent = `${stale ? "Avläsningen är inte aktuell. " : ""}Senast avläst: ${checked} · Uppdateras automatiskt${state.wait_seconds ? ` · Nästa ändring tidigast om ${Math.ceil(state.wait_seconds / 60)} min` : ""}`;
    if (stale) {
      $("status-title").textContent = "Väntar på aktuell avläsning";
      $("status-message").textContent = state.error_message || "Temperaturerna visas inte som aktuella. Försöker igen automatiskt.";
    }
    if (state.contributions) {
      const c = state.contributions;
      const shift = state.target_shift ? `${state.target_shift > 0 ? "Förvärmer före dyrt elpris" : "Dyrt elpris"}: målet är tillfälligt ${displayNumber(settings.target_temperature + state.target_shift)} °C. ` : "";
      const note = state.preheat?.note ? `${state.preheat.note} ` : "";
      const smoothed = typeof state.indoor_smoothed === "number" ? `Rumsregeln använder timmedel ${displayNumber(state.indoor_smoothed)} °C; aktuell temperatur visas ovan. ` : "";
      $("explanation").textContent = `${shift}${note}${smoothed}Bas${c.base_correction ? ` + auto (${displayNumber(c.base_correction)})` : ""} + rum (${displayNumber(c.room)}) + väder (${displayNumber(c.weather)}) + elpris (${displayNumber(c.price)}) + kamin (${displayNumber(c.fireplace ?? 0)}) → begränsat mål ${displayNumber(state.desired_offset)}. Nästa steg anpassas till pumpens steglängd och dina gränser.`;
    } else {
      $("explanation").textContent = "Inget aktuellt förslag. Kontrollera inställningarna och statusmeddelandet.";
    }
  } catch (error) {
    $("status-title").textContent = "Kontakt med tillägget saknas";
    $("status-message").textContent = error.message;
    lastStatus = null;
    renderVacation();
    renderFireplace();
    renderHotWaterControl();
    renderAuxLimitControl();
    renderRoomImpact();
    for (const id of ["indoor", "outdoor-now", "current-offset", "hero-offset", "desired-offset", "next-offset"]) $(id).textContent = "—";
    $("hero-offset-base").textContent = "";
    $("indoor-diff").textContent = "Medel av dina valda rum";
    $("next-change").textContent = "Avvaktar";
    $("next-change-why").textContent = "Kontakt med tillägget saknas.";
    $("adjust-offset-limits").hidden = true;
    $("updated").textContent = "Visningen är inte aktuell. Ansluter igen automatiskt.";
  } finally {
    statusBusy = false;
    if (pendingRefreshes.delete("status")) void refreshStatus();
  }
}

async function refreshSavedSettings() {
  if (busy || settingsBusy || editingSettings()) return;
  settingsBusy = true;
  const revision = settingsRevision;
  try {
    const latest = await api("settings");
    if (busy || revision !== settingsRevision) return;
    if (JSON.stringify(latest) !== JSON.stringify(settings)) {
      const draft = readSettings();
      const merged = { ...latest };
      const roomFields = ["room_sensors", "priority_sensors", "nearest_thermostat", "fireplace_rooms", "fireplace_enabled"];
      const roomDraft = roomFields.some((key) => JSON.stringify(draft[key]) !== JSON.stringify(settings[key]));
      const priceFields = ["price_provider", "price_entity"];
      const priceDraft = priceFields.some((key) => draft[key] !== settings[key]);
      for (const key of Object.keys(settings)) {
        if ((roomDraft && roomFields.includes(key)) || (priceDraft && priceFields.includes(key))
          || JSON.stringify(draft[key]) !== JSON.stringify(settings[key])) merged[key] = draft[key];
      }
      const energyChanged = ["offset_entity", "energy_entity", "house_power_entity", "price_entity", "price_provider", "max_age_minutes",
        "grid_import_entity", "grid_export_entity", "solar_entity", "battery_discharge_entity", "battery_charge_entity"]
        .some((key) => latest[key] !== settings[key]);
      settings = latest;
      renderSettings(merged, true);
      if (energyChanged) { void refreshEnergy(); void refreshAuxiliary(); void refreshSources(); void refreshSavings(); }
    }
    renderSavedSettingsStatus();
  } catch (error) {
    if (!busy && revision === settingsRevision) $("settings-sync-status").textContent = `Sparade inställningar kunde inte uppdateras: ${error.message}. Försöker igen automatiskt.`;
  } finally {
    settingsBusy = false;
  }
}

function renderSavedSettingsStatus() {
  $("settings-sync-status").textContent = `Sparat mål: ${displayNumber(settings.target_temperature)} °C. Osparade ändringar används först när du sparar.`;
  $("offset-limits-saved").textContent = `Sparat intervall: ${displayNumber(settings.min_offset)} till ${displayNumber(settings.max_offset)} steg.`;
}

function renderIndoorDiff() {
  const indoor = lastStatus?.indoor, target = settings?.target_temperature;
  const priorities = $("priority_sensors")?.querySelectorAll("input:checked").length
    ?? settings?.priority_sensors.length ?? 0;
  const rooms = $("room_sensors")?.querySelectorAll("input:checked").length
    ?? settings?.room_sensors.length ?? 0;
  const label = settings?.priority_sensors.length ? "Snitt för prioriterade rum" : "Medel av dina valda rum";
  $("target-label").textContent = priorities ? "Mål för prioriterade rum · °C" : "Mål för valda rum · °C";
  $("target-scope").textContent = priorities
    ? `Målet gäller snittet av ${priorities} ${priorities === 1 ? "prioriterat rum" : "prioriterade rum"}.`
    : rooms ? `Utan prioritering gäller målet snittet av alla ${rooms} valda rum.`
      : "Välj prioriterade rum eller rumsgivare för att ange vad målet gäller.";
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
  renderPreheatExample();
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
  const limited = limit && !lastStatus.error_message && lastStatus.checked_at
    && !confirmedShortRuntimeAlarm()
    && Date.now() / 1000 - lastStatus.checked_at <= 180
    && lastStatus.next_offset === lastStatus.current_offset;
  $("adjust-offset-limits").hidden = !limited;
  if (!limited) return;
  $("next-change").textContent = limit.direction === "upper" ? "Övre gränsen nådd" : "Nedre gränsen nådd";
  $("next-change-why").textContent = `Beräknad förskjutning ${oneDecimal(limit.requested)}, men din sparade gräns är ${displayNumber(limit.value)}. Automatiken stannar vid gränsen. Du kan ändra tillåtet intervall i inställningarna.`;
}

function confirmedShortRuntimeAlarm() {
  const alarm = lastStatus?.pump_alarm;
  return alarm?.active === true && alarm.code === 229 && !alarm.error;
}

function renderLiveAdjustment() {
  renderControlLimit();
  if (!lastStatus?.checked_at || Date.now() / 1000 - lastStatus.checked_at > 180) {
    $("next-change").textContent = "Avvaktar";
    $("next-change-why").textContent = "Aktuellt underlag saknas.";
  } else if (confirmedShortRuntimeAlarm()) {
    $("next-change").textContent = "Pumplarm 229";
    $("next-change-why").textContent = "Kurv- och termostatstyrningen väntar tills larmet är åtgärdat. Pumpens skydd återställs inte av appen.";
  } else if (lastStatus.pending_write?.error) {
    $("next-change").textContent = "Återförsök väntar";
    $("next-change-why").textContent = `Återläser först; nytt försök tidigast om ${Math.ceil((lastStatus.wait_seconds || 0) / 60)} min om ändringen fortfarande behövs.`;
  } else if (lastStatus.error_message) {
    $("next-change").textContent = "Avvaktar";
    $("next-change-why").textContent = lastStatus.error_message;
  } else if (lastStatus.pending_write) {
    $("next-change").textContent = "Inväntar återläsning";
    $("next-change-why").textContent = "Ändringen är begärd, men har ännu inte bekräftats av Home Assistant.";
  }
}

function renderSummary(data) {
  summaryPlan = data;
  const coming = (data.outdoor.forecast || []).filter((point) => point[0] >= data.now && point[0] <= data.now + 86400 && point[1] !== null);
  if (coming.length) {
    const coldest = coming.reduce((a, b) => (b[1] < a[1] ? b : a));
    const warmest = coming.reduce((a, b) => (b[1] > a[1] ? b : a));
    $("outdoor-forecast").textContent = `Närmaste dygnet ${displayNumber(Math.round(coldest[1]) || 0)} till ${displayNumber(Math.round(warmest[1]) || 0)} °C · kallast ${timeLabel(coldest[0], 86400 * 2)}`;
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
  renderLiveAdjustment();
  renderHomeHistory();
}

async function save(config) {
  if (busy) return;
  settingsRevision++;
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
    void refreshEnergy();
    void refreshAuxiliary(); void refreshSources();
    void refreshSavings();
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
  if (Math.min(...values) >= 0 && minimum < 0) minimum = 0;
  if (options.integer !== false) { minimum = Math.floor(minimum); maximum = Math.ceil(maximum); }
  if (maximum - minimum < 1e-9) { minimum -= 1; maximum += 1; }
  const step = niceStep(maximum - minimum, 4);
  minimum = Math.floor(minimum / step + 1e-9) * step;
  maximum = Math.ceil(maximum / step - 1e-9) * step;
  const width = chartWidth(container), narrow = width < 520;
  const height = options.height ?? (narrow ? 220 : 280);
  const left = options.left ?? (narrow ? 46 : 56), right = narrow ? 8 : 20, top = markers.length ? 40 : 16, bottom = 30;
  const ticks = narrow ? 2 : 4;
  const x = (time) => left + (Math.max(start, Math.min(end, time)) - start) / (end - start) * (width - left - right);
  const y = (value) => top + (maximum - value) / (maximum - minimum) * (height - top - bottom);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  for (let value = minimum; value <= maximum + step / 1000; value += step) {
    svg.append(svgElement("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "chart-grid" }));
    svg.append(svgElement("text", { x: left - 6, y: y(value) + 4, "text-anchor": "end" }, `${displayNumber(Math.round(value / step) * step)}${unit}`));
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

// Same kWh at the period's average price minus what they cost: a gain from timing, not from using less.
function priceShiftMetric(shift, currency) {
  if (!shift) return metric({ label: "Prisflytt", value: null, detail: "Kräver timmar med både mätning och pris." });
  const gain = shift.difference >= 0;
  return metric({
    label: `${gain ? "Tjänat på prisflytt" : "Dyrare än snittpris"} · ${shift.complete ? "hela perioden" : `${shift.hours} av 24 timmar`}`,
    value: displayNumber(Math.abs(shift.difference)), unit: currency,
    detail: `Snittpris ${displayNumber(shift.average_price)}, pumpens pris ${displayNumber(shift.actual_price)} ${currency}/kWh`,
  });
}

function metric(item) {
  const block = document.createElement("div");
  block.className = "live-metric";
  if (item.active === true) block.classList.add("alarm-active");
  const label = document.createElement("span");
  label.textContent = item.label;
  const value = document.createElement("strong");
  // Home Assistant states are raw strings ("36.8"); show numbers the Swedish way like the rest of the page.
  const shown = typeof item.value === "string" && /^-?\d+(\.\d+)?$/.test(item.value) ? displayNumber(Number(item.value)) : operationText(item.value);
  const latest = item.code != null ? (item.code_name ? `${item.code} · ${item.code_name}` : `Kod ${item.code}`) : null;
  const cleared = item.code != null && item.active === false;
  value.textContent = item.value === null ? "Okänt" : cleared ? "Inget aktivt larm" : latest ?? `${shown} ${item.unit || ""}`.trim();
  const detail = document.createElement("small");
  const since = item.since ? ` · sedan ${new Date(item.since * 1000).toLocaleDateString("sv-SE", { day: "numeric", month: "short" })}` : "";
  const via = item.source ? ` enligt ${item.source}${item.source_checked_at ? ` (${dayAndTime(item.source_checked_at)})` : ""}` : "";
  detail.textContent = item.error || item.detail || (cleared ? (item.code ? `Senaste larmkod ${latest}${via}` : item.source ? `Enligt ${item.source}` : "") : item.active === true && item.key === "alarm" ? `Aktivt larm${via}` : item.code != null ? `Senaste larmkod${since} · aktiv status saknas` : "")
    || (item.reported_at ? `Rapporterat ${new Date(item.reported_at).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}` : "");
  if (item.entity_id) block.title = item.entity_id;
  block.append(label, value, detail);
  return block;
}

let alarmsBusy = false, alarmActionBusy = false, lastAlarm = null;
let cloudAlarmsBusy = false, cloudResetBusy = false, cloudAlarmReport = null;
let cloudDiscoveryBusy = false, cloudChoices = [];

async function discoverCloudDevices() {
  if (cloudDiscoveryBusy || cloudResetBusy) return;
  cloudDiscoveryBusy = true;
  $("discover-cloud-devices").disabled = $("save-cloud-device").disabled = true;
  $("cloud-device-choice").disabled = true;
  try {
    const data = await api("myuplink/discovery");
    cloudChoices = data.choices;
    const select = $("cloud-device-choice");
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Välj rätt anläggning och enhet";
    select.append(placeholder);
    cloudChoices.forEach((choice, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = `${choice.system_name} · ${choice.device_name} · ${choice.system_id} / ${choice.device_id}`;
      select.append(option);
    });
    const savedIndex = cloudChoices.findIndex((c) => c.system_id === data.system_id && c.device_id === data.device_id);
    select.value = savedIndex >= 0 ? String(savedIndex) : cloudChoices.length === 1 && !data.system_id && !data.device_id ? "0" : "";
    select.disabled = !cloudChoices.length || data.manual_selection;
    $("save-cloud-device").disabled = select.disabled || !select.value;
    $("cloud-discovery-status").textContent = !data.credentials_configured
      ? "Ange client ID och client secret i tilläggets Konfiguration först."
      : data.manual_selection ? "Manuella ID:n används. Töm system-ID/device-ID i tilläggskonfigurationen och starta om om du vill spara pumpval här."
      : `${cloudChoices.length} enheter hittades. ${savedIndex >= 0 ? "Sparat pumpval visas." : data.system_id ? "Sparat pumpval saknas i listan och har inte ändrats." : "Kontrollera valet och spara; inget pumpval ändras av hämtningen."}`;
  } catch (error) {
    cloudChoices = [];
    $("cloud-device-choice").replaceChildren();
    $("cloud-discovery-status").textContent = `Kunde inte hämta anläggningar: ${error.message}`;
  } finally {
    cloudDiscoveryBusy = false;
    $("discover-cloud-devices").disabled = false;
  }
}

async function saveCloudDevice() {
  if (cloudDiscoveryBusy || cloudResetBusy || cloudAlarmsBusy || $("save-cloud-device").disabled) return;
  const choice = cloudChoices[Number($("cloud-device-choice").value)];
  if (!choice || !window.confirm(`Spara ${choice.system_name} / ${choice.device_name} (${choice.system_id} / ${choice.device_id}) för molnlarmläsning? Detta ändrar inte HA-pumpvalet och kvitterar inga larm.`)) return;
  cloudDiscoveryBusy = true;
  $("save-cloud-device").disabled = $("discover-cloud-devices").disabled = true;
  try {
    await api("myuplink/discovery", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ system_id: choice.system_id, device_id: choice.device_id }) });
    cloudAlarmReport = null;
    $("cloud-alarms").replaceChildren();
    $("cloud-discovery-status").textContent = "Pumpvalet är sparat och gäller även efter omstart. Inget larm har kvitterats.";
    await refreshCloudAlarms();
  } catch (error) {
    $("cloud-discovery-status").textContent = `Pumpvalet kunde inte bekräftas: ${error.message}. Hämta anläggningar igen och kontrollera det sparade valet.`;
  } finally {
    cloudDiscoveryBusy = false;
    $("discover-cloud-devices").disabled = false;
  }
}

async function refreshCloudAlarms() {
  if (cloudAlarmsBusy || cloudResetBusy) return;
  cloudAlarmsBusy = true;
  $("cloud-alarms").querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const data = await api("myuplink/alarms");
    cloudAlarmReport = data;
    $("cloud-alarm-status").textContent = data.configured
      ? `Direkt myUplink · system ${data.system_id} · device ${data.device_id}. ${data.alarms.length} larm för denna enhet. Lästa ${new Date(data.checked_at * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}; läsningen kan återanvändas i en minut. Detta pumpval är separat från HA-valet.`
      : "Direkt myUplink är inte konfigurerat. Fyll i anslutningsuppgifterna under tilläggets Konfiguration.";
    $("cloud-alarms").replaceChildren(...data.alarms.map((alarm) => {
      const row = document.createElement("li");
      const title = document.createElement("p");
      title.textContent = `${alarm.title} · kod ${alarm.code} · ${alarm.status} · ID ${alarm.id}`;
      row.append(title);
      if (alarm.can_reset) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Kvittera larm 229";
        button.addEventListener("click", () => void resetCloudAlarm(alarm.id));
        row.append(button);
      } else {
        const reason = document.createElement("p");
        reason.textContent = alarm.blocked_reason;
        row.append(reason);
      }
      return row;
    }));
  } catch (error) {
    cloudAlarmReport = null;
    $("cloud-alarms").replaceChildren();
    $("cloud-alarm-status").textContent = `Kunde inte läsa myUplink-larm: ${error.message}`;
  } finally {
    cloudAlarmsBusy = false;
  }
}

async function resetCloudAlarm(id) {
  if (cloudResetBusy || cloudAlarmsBusy || cloudDiscoveryBusy || !cloudAlarmReport?.alarms.some((alarm) => alarm.id === id && alarm.can_reset)) return;
  if (!window.confirm(`Begär manuell kvittering av larm 229 för myUplink-enhet ${cloudAlarmReport.device_id} i system ${cloudAlarmReport.system_id}?\n\nBekräfta att du kontrollerat och åtgärdat orsaken enligt NIBE. Kompressorn kan starta igen. Kvitteringen löser inte flödes- eller hårdvarufel.`)) return;
  cloudResetBusy = true;
  $("cloud-alarms").querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const result = await api("myuplink/alarms", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "reset", id, confirmed: true,
        system_id: cloudAlarmReport.system_id, device_id: cloudAlarmReport.device_id }) });
    $("cloud-alarm-action").textContent = result.message;
  } catch (error) {
    $("cloud-alarm-action").textContent = `Kvitteringen kunde inte bekräftas: ${error.message}`;
  } finally {
    cloudResetBusy = false;
    await refreshCloudAlarms();
    void refreshAlarms();
  }
}

let compressorDiagnosticsBusy = false;
let compressorDiagnosticsData = null;
let compressorParameterSelection = "";

function diagnosticSnapshot(snapshot) {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `Visa pumpvärden · ${dayAndTime(snapshot.timestamp)}`;
  details.append(summary);
  const values = document.createElement("ul");
  values.className = "plan-changes";
  for (const item of snapshot.pump || []) {
    const row = document.createElement("li");
    const value = item.value === null ? "okänt" : `${item.value}${item.unit ? ` ${item.unit}` : ""}`;
    const age = item.age_seconds === null ? "rapportålder okänd" : `senast rapporterad ${item.age_seconds} s före provet`;
    row.textContent = `${item.name}: ${value} · ${age}`;
    values.append(row);
  }
  const app = snapshot.app || {};
  const controls = document.createElement("p");
  const context = [
    app.price_level ? `Elpris: ${app.price_level}` : null,
    app.price !== null && app.price !== undefined ? `pris ${displayNumber(Number(app.price))}` : null,
    app.target_shift !== null && app.target_shift !== undefined ? `pris-/förvärmningsbidrag ${displayNumber(Number(app.target_shift))}°` : null,
    app.desired_offset !== null && app.desired_offset !== undefined ? `önskat offset ${displayNumber(Number(app.desired_offset))}` : null,
    app.current_offset !== null && app.current_offset !== undefined ? `aktuellt offset ${displayNumber(Number(app.current_offset))}` : null,
  ].filter(Boolean);
  controls.className = "hint";
  controls.textContent = context.length ? `NIBE Comfort vid provet: ${context.join(" · ")}` : "Ingen extra styrkontext rapporterad vid provet.";
  details.append(controls, values);
  return details;
}

function renderCompressorDiagnostics(data) {
  compressorDiagnosticsData = data;
  const status = $("compressor-diagnostics-status");
  const metrics = $("compressor-diagnostics-metrics");
  $("compressor-auto-reset-status").textContent = data.auto_reset_enabled
    ? "Automatisk kvittering är PÅ. Kräver 30 min larmfri övervakning före larmet, ett komplett 30+10-minuters analysfönster, aktivt 229 med ett numeriskt ID och en enda begäran utan återförsök."
    : "Automatisk kvittering är AV (standard). Endast den manuella, bekräftade myUplink-kvitteringen kan användas.";
  const stateNames = {
    pending: "Väntar på analysfönstret", attempting: "En engångsbegäran pågår",
    submitted: "Begäran skickad – pumpens status är inte bekräftad",
    failed: "Begäran misslyckades – inget återförsök görs",
    blocked: "Automatisk kvittering spärrad", cleared: "Larmet försvann före analysen",
  };
  $("compressor-auto-reset-history").replaceChildren(...(data.auto_resets || []).map((item) => {
    const article = document.createElement("article");
    article.className = "event";
    const time = document.createElement("small");
    time.textContent = `Larm-ID ${item.alarm_id} · första observation ${dayAndTime(item.first_seen)}`;
    const title = document.createElement("strong");
    title.textContent = stateNames[item.state] || item.state;
    const message = document.createElement("p");
    message.textContent = item.message;
    article.append(time, title, message);
    for (const recommendation of item.analysis?.recommendations || []) {
      const line = document.createElement("p");
      line.className = "hint";
      line.textContent = recommendation;
      article.append(line);
    }
    return article;
  }));
  const recommendations = data.recommendations || [];
  $("compressor-recommendations").replaceChildren(...recommendations.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
  $("compressor-diagnostics-quality").textContent = data.available
    ? `${data.sample_count} prover · provintervall typiskt ${data.sample_interval_seconds ?? "—"} s · största lucka ${data.max_gap_seconds ?? "—"} s · ${data.gaps_over_120_seconds} luckor över 2 minuter · ${data.samples_without_pump_values} prover utan pumpvärden. ${data.message}${data.truncated ? " Pumpens entitetslista är avkortad." : ""}`
    : data.message;
  if (!data.available) {
    status.textContent = data.message;
    metrics.replaceChildren();
    $("compressor-temperature-chart").textContent = "Inväntar pumpvärden.";
    $("compressor-temperature-legend").replaceChildren();
    $("compressor-parameter-chart").replaceChildren();
    $("compressor-cycles").textContent = "Inga körningar kan visas ännu.";
    $("compressor-alarm-events").textContent = "Inga larm kan visas ännu.";
    $("compressor-latest-values").replaceChildren();
    fillSelect($("compressor-parameter"), [new Option("Inga numeriska pumpvärden", "")], "");
    return;
  }
  status.textContent = `${data.sample_count} prover från ${dayAndTime(data.start)} till ${dayAndTime(data.end)}.`;
  const summaryMetrics = [
    { label: "Prover", value: String(data.sample_count) },
    { label: "Typiskt intervall", value: data.sample_interval_seconds === null ? "—" : `${data.sample_interval_seconds} s` },
    { label: "Största lucka", value: data.max_gap_seconds === null ? "—" : `${Math.round(data.max_gap_seconds / 60)} min` },
    { label: "Prover utan pumpdata", value: String(data.samples_without_pump_values) },
    { label: "Körningar", value: String(data.cycles.length) },
  ];
  metrics.replaceChildren(...summaryMetrics.map((item) => metric(item)));

  const markers = [];
  for (const cycle of data.cycles.slice(-20)) {
    markers.push({ time: cycle.start, label: "Kompressor start", short: "Start", className: "diagnostic-cycle",
      title: `Kompressor påvisad ${dayAndTime(cycle.start)}` });
    markers.push({ time: cycle.end, label: "Kompressor stopp", short: "Stopp", className: "diagnostic-cycle",
      title: `Kompressor avläst av ${dayAndTime(cycle.end)}` });
  }
  for (const alarm of data.alarms) {
    markers.push({ time: alarm.timestamp, label: alarm.code === 229 ? "Larm 229" : "Larm", short: "Larm",
      className: "diagnostic-alarm", title: `Aktivt larm${alarm.code ? ` ${alarm.code}` : ""} avläst ${dayAndTime(alarm.timestamp)}` });
  }
  const temperatureSeries = data.temperatures.map((item, index) => ({
    name: item.name, points: item.points, color: index,
  }));
  const validRange = Number.isFinite(data.start) && Number.isFinite(data.end) && data.end > data.start;
  if (temperatureSeries.length && validRange) {
    drawChart($("compressor-temperature-chart"), temperatureSeries, {
      start: data.start, end: data.end, markers, unit: "°", pad: 1, empty: "Inga temperaturvärden i perioden.",
    });
    legend($("compressor-temperature-legend"), temperatureSeries);
  } else {
    $("compressor-temperature-chart").textContent = "Temperaturgrafen visas när minst två tidspunkter har rapporterats.";
    $("compressor-temperature-legend").replaceChildren();
  }

  const select = $("compressor-parameter");
  const current = select.value || compressorParameterSelection;
  const options = data.series.map((item) => new Option(
    `${item.name}${item.unit ? ` · ${item.unit}` : ""}`, item.entity_id,
  ));
  const fallback = data.series.find((item) => item.entity_id.replace(/_\d+$/, "").endsWith("_return_line_bt3"))
    || data.series.find((item) => item.entity_id.replace(/_\d+$/, "").endsWith("_supply_line_bt2"))
    || data.series.find((item) => !data.temperatures.some((temperature) => temperature.entity_id === item.entity_id))
    || data.series[0];
  compressorParameterSelection = data.series.some((item) => item.entity_id === current) ? current : fallback?.entity_id || "";
  fillSelect(select, options.length ? options : [new Option("Inga numeriska pumpvärden", "")], compressorParameterSelection);
  const chosen = data.series.find((item) => item.entity_id === select.value);
  if (chosen && validRange) {
    const unit = chosen.unit || "";
    const lastValue = chosen.points[chosen.points.length - 1]?.[1] || 1;
    drawChart($("compressor-parameter-chart"), [{ name: chosen.name, points: chosen.points }],
      { start: data.start, end: data.end, markers, unit, pad: Math.max(0.1, Math.abs(lastValue) * 0.03), integer: false });
  } else {
    $("compressor-parameter-chart").textContent = "Värdegrafen visas när minst två tidspunkter har rapporterats.";
  }

  const cycles = $("compressor-cycles");
  cycles.replaceChildren(...[...data.cycles].reverse().slice(0, 30).map((cycle) => {
    const article = document.createElement("article");
    article.className = "event";
    const time = document.createElement("small");
    time.textContent = `${dayAndTime(cycle.start)} – ${dayAndTime(cycle.end)} · observerat ${Math.round(cycle.observed_seconds / 60)} min`;
    const title = document.createElement("strong");
    const maximum = cycle.maximum_seconds === null ? "okänt" : `${Math.round(cycle.maximum_seconds / 60)}`;
    title.textContent = `${cycle.classification} · möjligt gångtidsintervall ${Math.round(cycle.minimum_seconds / 60)}–${maximum} min`;
    article.append(time, title, diagnosticSnapshot(cycle.start_sample), diagnosticSnapshot(cycle.end_sample));
    return article;
  }));
  if (!data.cycles.length) cycles.textContent = "Ingen fullständig start/stopp-körning i perioden.";

  const alarms = $("compressor-alarm-events");
  alarms.replaceChildren(...[...data.alarms].reverse().map((alarm) => {
    const article = document.createElement("article");
    article.className = "event";
    const title = document.createElement("strong");
    title.textContent = alarm.code ? `Aktivt larm ${alarm.code}` : "Aktivt larm (kod saknas)";
    const time = document.createElement("small");
    time.textContent = `Första aktiva provet ${dayAndTime(alarm.timestamp)}`;
    article.append(time, title, diagnosticSnapshot(alarm.sample));
    return article;
  }));
  if (!data.alarms.length) alarms.textContent = "Inga övergångar till aktivt HA-larm har fångats i perioden.";
  $("compressor-latest-values").replaceChildren(...data.entities.map((item) => metric({
    label: `${item.name} · prov ${dayAndTime(item.sampled_at)}`,
    value: item.value === null ? "Okänt" : item.value, unit: item.unit || "",
  })));
}

async function refreshCompressorDiagnostics() {
  if (compressorDiagnosticsBusy) return;
  compressorDiagnosticsBusy = true;
  $("refresh-compressor-diagnostics").disabled = true;
  try {
    const hours = $("compressor-diagnostics-hours").value;
    renderCompressorDiagnostics(await api(`compressor-diagnostics?hours=${encodeURIComponent(hours)}`));
  } catch (error) {
    $("compressor-diagnostics-status").textContent = `Kunde inte läsa kompressordiagnostiken: ${error.message}`;
  } finally {
    compressorDiagnosticsBusy = false;
    $("refresh-compressor-diagnostics").disabled = false;
  }
}

async function refreshAlarms() {
  if (alarmsBusy || alarmActionBusy) return;
  alarmsBusy = true;
  $("alarm-pause").disabled = true;
  try {
    const data = await api("alarms");
    lastAlarm = data;
    $("alarm-status").textContent = data.error
      ? `Larmstatus är osäker: ${data.error}`
      : data.active === true ? `Aktivt pumplarm${data.code !== null ? ` ${data.code}${data.info ? ` ${data.info.name}` : ""}` : ""}`
      : data.active === false ? "Inget aktivt pumplarm rapporterat." : "Larmstatus är okänd.";
    if (data.source && data.source_checked_at) $("alarm-status").textContent += ` Enligt ${data.source}, läst ${dayAndTime(data.source_checked_at)}.`;
    $("alarm-explanation").textContent = data.explanation;
    // Operating values and rooms help troubleshoot an active alarm; without one they repeat the pump card.
    const active = data.active === true;
    $("alarm-observations").replaceChildren(...(active ? data.observations.filter((item) => item.value !== null).map(metric) : []));
    $("alarm-pause").hidden = !data.can_pause;
    $("alarm-rooms").replaceChildren(...(active || data.can_pause ? data.rooms : []).map((room) => {
      const item = document.createElement("li");
      item.textContent = `${room.name}: ${displayNumber(room.temperature)} °C${room.setpoint == null ? "" : ` · börvärde ${displayNumber(room.setpoint)} °C`}${room.error ? ` · ${room.error}` : ""}`;
      return item;
    }));
  } catch (error) {
    lastAlarm = null;
    $("alarm-pause").hidden = true;
    $("alarm-rooms").replaceChildren();
    $("alarm-observations").replaceChildren();
    $("alarm-explanation").textContent = "";
    $("alarm-status").textContent = `Kunde inte läsa larm: ${error.message}. Försöker igen automatiskt.`;
  } finally {
    alarmsBusy = false;
    $("alarm-pause").disabled = alarmActionBusy || !lastAlarm?.can_pause;
  }
}

async function pauseAlarmThermostats() {
  if (busy || alarmActionBusy || alarmsBusy || !lastAlarm?.can_pause) return;
  if (!window.confirm("Pausa appens termostatbegränsning? Detta öppnar inte ventiler, ändrar inga befintliga börvärden och återställer inte pumpens larm.")) return;
  alarmActionBusy = true;
  busy = true;
  $("save").disabled = $("save-near").disabled = $("pause").disabled = $("save-target").disabled = true;
  $("alarm-pause").disabled = true;
  try {
    await api("alarms", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "pause_thermostats" }) });
    settings.thermostat_control_enabled = false;
    $("thermostat_control_enabled").checked = false;
    settingsRevision++;
    updateSaveState();
    $("alarm-action-status").textContent = "Termostatbegränsning pausad. Befintliga börvärden är oförändrade; pumpens larm är inte återställt.";
  } catch (error) {
    $("alarm-action-status").textContent = `Åtgärden misslyckades: ${error.message}. Kontrollera status innan du försöker igen.`;
  } finally {
    alarmActionBusy = false;
    busy = false;
    $("save").disabled = $("save-near").disabled = $("pause").disabled = $("save-target").disabled = false;
    await refreshAlarms();
    void refreshStatus();
  }
}

async function refreshOverview() {
  if (overviewBusy) return;
  overviewBusy = true;
  $("refresh-overview").disabled = true;
  void refreshAlarms();
  void refreshCloudAlarms();
  const provider = settings.price_provider;
  try {
    const data = await api("overview");
    if (provider !== settings.price_provider) return;
    // Keep the main panel to actionable pump state; secondary readings stay available on demand.
    const known = data.pump.filter((item) => item.key === "alarm" || item.value !== null || item.active === true);
    const missing = data.pump.filter((item) => !known.includes(item));
    const primaryKeys = new Set(["alarm", "compressor", "operation", "additional_heat"]);
    const primary = known.filter((item) => primaryKeys.has(item.key));
    const extra = known.filter((item) => !primaryKeys.has(item.key));
    const renderPumpMetric = (item) => {
      const block = metric(item);
      const value = String(item.value ?? "");
      block.dataset.state = item.active === true ? "alarm" : /^(I drift|Värme|Varmvatten)$/.test(value) ? "on"
        : /^(Av|Stoppad|Vilar|Viloläge)$/.test(value) ? "off" : "value";
      return block;
    };
    $("pump-metrics").replaceChildren(...primary.map(renderPumpMetric));
    $("pump-extra-metrics").replaceChildren(...extra.map(renderPumpMetric));
    $("pump-extra-summary").textContent = `Visa fler pumpvärden · ${extra.length}`;
    $("pump-extra-details").hidden = !extra.length;
    $("pump-missing").textContent = missing.map((item) => `${item.label}: ${item.error || "Aktuellt värde saknas"}`).join(" · ");
    $("pump-missing-summary").textContent = `${missing.length} pumpvärden saknas eller är gamla`;
    $("pump-missing-details").hidden = !missing.length;
    const compressor = data.pump.find((item) => item.label === "Kompressor")?.value;
    const [tone, text] = data.pump.some((item) => item.active === true) ? ["red", "Larm"]
      : compressor === "I drift" ? ["green", "Kompressor i drift"]
      : compressor ? ["muted", `Kompressor: ${compressor.toLowerCase()}`] : ["muted", "Okänt läge"];
    featureTone("pump-card", "pump-pill", tone, text);
    $("electricity-metrics").replaceChildren(...data.pump_electricity.map(metric), ...data.house.map(metric));
    if (data.price) $("electricity-metrics").append(metric(data.price));
    if (!data.pump_electricity.length) $("electricity-metrics").prepend(metric({
      label: "Pumpens elförbrukning", value: "Ingen egen mätare", detail: "Uppskattningen finns under Energi.",
    }));
    $("electricity-note").textContent = data.electricity_note;
    $("overview-status").textContent = `Senast hämtat ${new Date(data.checked_at * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}.`;
  } catch (error) {
    if (provider !== settings.price_provider) return;
    $("pump-metrics").replaceChildren();
    $("pump-extra-metrics").replaceChildren();
    $("pump-extra-details").hidden = true;
    $("pump-missing-details").hidden = true;
    featureTone("pump-card", "pump-pill", "muted", "Kan inte läsas");
    $("electricity-metrics").replaceChildren();
    const homeAssistantUnauthorized = /Home Assistant svarade med HTTP 401/.test(error.message);
    const ingressUnauthorized = error.status === 401;
    const guidance = homeAssistantUnauthorized
      ? "Home Assistant nekar tilläggets Supervisor-token. Kontrollera tilläggsloggen; om felet kvarstår, starta om NIBE Comfort-tillägget."
      : ingressUnauthorized
        ? "Home Assistant nekade webbpanelens inloggningssession. Öppna tillägget igen från sidomenyn och uppdatera."
        : error.message;
    $("overview-status").textContent = `Kunde inte läsa pumpstatus: ${guidance}`;
  } finally {
    overviewBusy = false;
    $("refresh-overview").disabled = false;
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
    text += c ? ` Beslutsbidrag i kurvsteg: rum ${displayNumber(c.room)}, utetemperatur ${displayNumber(c.weather)}, elpris ${displayNumber(c.price)}${c.fireplace == null ? "" : `, kamin ${displayNumber(c.fireplace)}`}.`
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
  // A confirmed request and the pump readback of the same value are one change on the chart.
  const confirmed = changes.filter((change) => change.status === "confirmed");
  const shown = changes.filter((change) => change.status !== "observed" || !confirmed.some((other) =>
    other.kind === change.kind && other.value === change.value && Math.abs(other.timestamp - change.timestamp) <= 900));
  return shown.map((change) => ({
    time: change.timestamp,
    label: `${change.kind === "offset" ? "K" : "T"} ${displayNumber(change.value)}${["confirmed", "observed"].includes(change.status) ? "" : " · begäran"}`,
    short: `${change.kind === "offset" ? "K" : "T"}${change.id}`,
    className: change.status === "confirmed" ? "done-auto" : "done-other",
    title: historyChangeText(change),
  }));
}

async function refreshEnergy() {
  if (energyBusy) { pendingRefreshes.add("energy"); return; }
  energyBusy = true;
  const signature = () => JSON.stringify([settings.offset_entity, settings.energy_entity, settings.house_power_entity, settings.price_entity, settings.price_provider, settings.max_age_minutes]);
  const requested = signature();
  $("energy-data").hidden = true;
  for (const id of ["energy-metrics", "energy-chart", "energy-cost-chart", "energy-table", "energy-comparisons"]) {
    chartRedraws.delete($(id));
    $(id).replaceChildren();
  }
  $("energy-price-status").textContent = "";
  try {
    $("energy-status").textContent = "Hämtar pumpens elhistorik…";
    const data = await api("energy");
    if (signature() !== requested) { pendingRefreshes.add("energy"); return; }
    const totals = data.totals;
    showEnergySection(true);
    $("energy-data").hidden = false;
    const covered = totals.coverage_seconds / 3600;
    const priced = totals.priced_seconds / 3600;
    $("energy-status").textContent = `${periodText(data.start, data.end)}. ${data.method === "estimate" ? estimateText(data.meter)
      : `${data.meter.automatic ? "Elmätare från myUplink" : "Vald elmätare"}: ${data.meter.name}. `}Data finns för ${displayNumber(covered)} av 24 timmar.${bridgedText(data.bridged_seconds)}${data.skipped_intervals ? ` Längre avbrott räknas inte med (${data.skipped_intervals} st).` : ""}${data.changes_error ? ` ${data.changes_error}` : ""}`;
    $("energy-metrics").replaceChildren(
      metric({ label: `${data.method === "power" || data.method === "estimate" ? "Uppskattad el" : "Använd el"} ·${totals.kwh === null ? "endast täckt del" : "hela perioden"}`,
        value: covered > 0 ? displayNumber(totals.measured_kwh) : null, unit: "kWh" }),
      metric({ label: totals.cost === null ? "Beräknad kostnad · endast prissatt del" : "Beräknad kostnad · hela perioden",
        value: priced > 0 ? displayNumber(totals.partial_cost) : null, unit: data.currency || "" }),
      priceShiftMetric(data.price_shift, data.currency || ""),
    );
    $("energy-price-status").textContent = `${data.price_entity ? `Elpris från ${data.price_provider === "nordpool" ? "Nord Pool" : "Tibber"}` : "Ingen sparad prisgivare"}, finns för ${displayNumber(priced)} av 24 timmar. ${data.method === "power" ? "El och kostnad uppskattas från eleffekt: senaste värdet gäller till nästa rapport, högst 15 minuter mellan rapporter. Ingen extrapolering efter sista rapporten. " : ""}${data.price_error || "Kostnaden är uppskattad utifrån mätarintervall och historiska priser."}`;
    const markers = [...historyMarkers(data.changes), ...data.settings_changes.map((change) => ({
      time: change.timestamp, label: "Inställningar sparade", short: "I", className: "done-other", title: change.label,
    }))];
    for (const [id, key, unit, name] of [
      ["energy-chart", "kwh", " kWh", "Använd el per timme · uppskattad fördelning"],
      ["energy-cost-chart", "cost", ` ${data.currency || ""}`, "Beräknad kostnad per timme"],
    ]) {
      drawChart($(id), [{ name, points: data.hours.map((hour) => [hour.start, hour[key]]), until: data.end }],
        { start: data.start, end: data.end, unit, integer: false, pad: 0.1, left: 96, markers,
          empty: "Inga fullständiga timvärden. Se datatäckning och givarval.", label: name });
    }
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    for (const label of ["Timme", "El · kWh", `Kostnad · ${data.currency || "valuta saknas"}`, "El/pris · min täckta"]) {
      const th = document.createElement("th");
      th.textContent = label;
      head.append(th);
    }
    const body = table.createTBody();
    for (const hour of data.hours) {
      const row = body.insertRow();
      for (const value of [
        new Date(hour.start * 1000).toLocaleString("sv-SE", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
        displayNumber(hour.kwh), displayNumber(hour.cost),
        `${displayNumber(hour.coverage_seconds / 60)} / ${displayNumber(hour.priced_seconds / 60)}`,
      ]) row.insertCell().textContent = value;
    }
    $("energy-table").append(table);
    const hourText = (hour) => !hour ? "timmen är inte klar än" : hour.kwh == null ? `${clock(hour.start)}: data saknas`
      : `${clock(hour.start)}: ${displayNumber(hour.kwh)} kWh, ${displayNumber(hour.cost)} ${data.currency || ""}`;
    const difference = (value) => `${value > 0 ? "+" : ""}${displayNumber(value)}`;
    const differenceText = (change) => change.difference_kwh == null ? "Skillnaden kan inte räknas ut."
      : `Skillnad (efter minus före): ${difference(change.difference_kwh)} kWh${change.difference_cost == null ? "" : `, ${difference(change.difference_cost)} ${data.currency || ""}`}.`;
    $("energy-comparisons").replaceChildren(...data.comparisons.map((change) => {
      const item = document.createElement("li");
      const label = change.kind === "settings" ? String(change.label || "").replace(/[.\s]+$/, "")
        : `${change.kind === "offset" ? "Kurvförskjutning" : change.entity_id} → ${displayNumber(change.value)} återläst`;
      item.textContent = `${clock(change.timestamp)} · ${label}. Före ${hourText(change.before)}. Efter ${hourText(change.after)}. ${differenceText(change)}`;
      return item;
    }));
    if (!data.comparisons.length) $("energy-comparisons").textContent = "Inga återlästa ändringar eller sparade inställningar under perioden.";
  } catch (error) {
    if (signature() !== requested) { pendingRefreshes.add("energy"); return; }
    $("energy-data").hidden = true;
    // Without any electricity meter for the pump the card has nothing to show; the sources card says how to add one.
    showEnergySection(!(!settings.energy_entity && /Ingen entydig/.test(error.message)));
    $("energy-status").textContent = `Kunde inte läsa elförbrukningen: ${error.message.replace(/[.!?]+\s*$/, "")}. Försöker igen automatiskt.`;
  } finally {
    energyBusy = false;
    if (pendingRefreshes.delete("energy")) void refreshEnergy();
  }
}

function showEnergySection(visible) {
  $("energy-section").hidden = !visible;
  document.querySelector('#classic-nav a[href="#energy-section"]').hidden = !visible;
}

function estimateText(meter) {
  const e = meter?.estimate;
  if (!e) return "";
  const c = e.calibration;
  const source = c?.kw ? `kalibrerat mot husets elförbrukning vid ${c.samples} kompressorstarter de senaste ${c.days} dagarna`
    : c?.error ? `standardvärde – kalibreringen misslyckades: ${c.error.replace(/[.!?]+\s*$/, "")}`
      : c ? `standardvärde – för få kompressorstarter för att kalibrera (${c.samples ?? 0} av ${c.starts ?? 0})`
        : "standardvärde";
  return `Pumpen har ingen elmätare, så elen är uppskattad, inte uppmätt: kompressorn räknas som ${displayNumber(e.compressor_kw)} kW när den går (${source}), plus elpatronens egen effekt och ${displayNumber(e.base_kw * 1000)} W för fläkt och cirkulationspump. `;
}

function savingsDate(period, monthly = false) {
  const [year, month, day] = period.split("-").map(Number);
  return monthly
    ? new Date(year, month - 1, 15).toLocaleDateString("sv-SE", { month: "long", year: "numeric" })
    : new Date(year, month - 1, day, 12).toLocaleDateString("sv-SE", { weekday: "short", day: "numeric", month: "short" });
}

// The before/after column (index 4) only means something when the change log has comparisons in the period;
// a new installation or one in monitoring mode has none, so the column is left out instead of a row of dashes.
function withoutEmptyActions(headers, rows) {
  if (rows.some((row) => row[4] !== "ingen ändring")) return [headers, rows];
  const drop = (values) => values.filter((_, index) => index !== 4);
  return [drop(headers), rows.map(drop)];
}

// Why some pump hours have no source split: no pump estimate, no house data, or the pump estimate exceeded the house.
function unallocatedText(data) {
  const house = new Map((data.house || []).map((hour) => [hour.start, hour.house_kwh]));
  const missing = (data.pump || []).filter((hour) => hour.grid_kwh == null);
  const over = missing.filter((hour) => hour.kwh != null && house.get(hour.start) != null && hour.kwh > house.get(hour.start));
  const count = missing.length === 1 ? "1 timme" : `${missing.length} timmar`;
  return ` ${count} av ${(data.pump || []).length} kunde inte fördelas${over.length === missing.length
    ? " eftersom pumpens uppskattade el var större än hela husets förbrukning den timmen" : over.length ? ", bland annat för att pumpens uppskattning var större än husets förbrukning" : " eftersom underlag saknades"}. Källor och kostnad gäller bara övriga timmar.`;
}

function savingsTable(container, headers, rows) {
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const label of headers) {
    const cell = document.createElement("th");
    cell.textContent = label;
    head.append(cell);
  }
  const body = table.createTBody();
  for (const values of rows) {
    const row = body.insertRow();
    for (const value of values) row.insertCell().textContent = String(value);
  }
  container.replaceChildren(table);
}

async function refreshSavings() {
  if (savingsBusy) { pendingRefreshes.add("savings"); return; }
  savingsBusy = true;
  const days = $("savings-days").value;
  $("savings-status").textContent = `Hämtar pumpens och elpatronens historik för ${days} dagar…`;
  $("savings-data").hidden = true;
  try {
    const data = await api(`savings?days=${encodeURIComponent(days)}`, { timeoutMs: 60000 });
    if ($("savings-days").value !== days) { pendingRefreshes.add("savings"); return; }
    const dailyHeater = new Map((data.heater?.daily || []).map((item) => [item.period, item]));
    const monthlyHeater = new Map((data.heater?.monthly || []).map((item) => [item.period, item]));
    const dailyActions = new Map(data.actions.daily.map((item) => [item.period, item]));
    const monthlyActions = new Map(data.actions.monthly.map((item) => [item.period, item]));
    const completeCost = (items) => items.filter((item) => item.cost !== null).reduce((sum, item) => sum + item.cost, 0);
    const hasCost = (items) => items.some((item) => item.cost !== null);
    const dailyShift = data.energy.daily.filter((item) => item.price_shift)
      .reduce((sum, item) => sum + item.price_shift.difference, 0);
    const hasShift = data.energy.daily.some((item) => item.price_shift);
    const timingDirection = !hasShift ? "Prisets tidpunkt"
      : dailyShift > 0 ? "Prisets tidpunkt · lägre kostnad än snittpris"
        : dailyShift < 0 ? "Prisets tidpunkt · högre kostnad än snittpris" : "Prisets tidpunkt · ingen skillnad mot snittpris";
    const totalCoverage = data.energy.daily.reduce((sum, item) => sum + item.complete_hours, 0);
    const totalPriceCoverage = data.energy.daily.reduce((sum, item) => sum + item.priced_complete_hours, 0);
    const totalHours = data.energy.daily.reduce((sum, item) => sum + item.total_hours, 0);
    const heaterDays = data.heater?.daily || [];
    const heaterCost = hasCost(heaterDays) ? completeCost(heaterDays) : null;
    const currency = data.energy.currency || "";
    $("savings-metrics").replaceChildren(
      metric({ label: "Pumpens faktiska kostnad · täckta timmar", value: hasCost(data.energy.daily) ? displayNumber(completeCost(data.energy.daily)) : null, unit: currency }),
      metric({ label: timingDirection, value: hasShift ? displayNumber(Math.abs(dailyShift)) : null,
        unit: currency, detail: "Jämför samma uppmätta kWh med dygnets snittpris. Visar inte om värmen eller förbrukningen ökade." }),
      metric({ label: "Jämförda styrändringar", value: displayNumber(data.actions.observations),
        unit: "tillfällen", detail: "Se kostnad och kWh före/efter för varje tillfälle nedan. Det är observationer, inte en summerad eller bevisad besparing." }),
      metric({ label: "Elpatronens kostnad · ingår i pumpens kostnad", value: heaterCost === null ? null : displayNumber(heaterCost), unit: currency }),
    );
    $("savings-status").textContent = `Senaste ${days} hela kalenderdagarna. ${data.energy.method === "estimate" ? estimateText(data.energy.meter) : `${data.energy.meter.name}. ${data.energy.method === "power" ? "Pumpens kWh uppskattas från effekthistorik. " : ""}`}${data.energy.price_error || "Kostnad räknad från vald prisgivare."}${data.heater_error ? ` Elpatron: ${data.heater_error}` : data.heater?.price_error ? ` Elpatron: ${data.heater.price_error}` : ""}`;
    $("savings-coverage").textContent = `Pumpens datatäckning: ${totalCoverage} av ${totalHours} timmar med fullständig elmätning, varav ${totalPriceCoverage} timmar även har fullständigt elpris. Dagar med luckor visar bara kompletta timmar, inte noll. Historiken beror på hur länge Home Assistant sparar Recorder-data. I tabellen betyder + lägre kostnad och − högre jämfört med respektive kolumns jämförelse.`;
    const heaterCostAt = (item) => item?.cost ?? null;
    const dateStamp = (period) => new Date(`${period}T12:00:00`).getTime() / 1000;
    drawChart($("savings-chart"), [
      { name: "Pumpens kostnad", points: data.energy.daily.map((item) => [dateStamp(item.period), item.cost]), until: data.end },
      { name: "Elpatron (ingår i pumpen)", points: data.energy.daily.map((item) => [dateStamp(item.period), heaterCostAt(dailyHeater.get(item.period))]), until: data.end },
    ], { start: data.start, end: data.end, unit: ` ${currency}`, integer: false, left: 96, pad: 0.1,
      empty: "Inga hela dagar med komplett elmätning och pris.", label: "Pumpens och elpatronens kostnad dag för dag" });
    const hasData = (item, heaterMap) => item.kwh != null || item.priced_complete_hours > 0 || heaterMap.get(item.period)?.kwh != null;
    const firstDay = data.energy.daily.findIndex((item) => hasData(item, dailyHeater));
    const dailyRows = (firstDay < 0 ? [] : data.energy.daily.slice(firstDay)).map((item) => {
      const heater = dailyHeater.get(item.period);
      const action = dailyActions.get(item.period);
      return [
        savingsDate(item.period), displayNumber(item.kwh), displayNumber(item.cost), displayNumber(item.price_shift?.difference),
        action ? `${action.estimated_savings > 0 ? "+" : ""}${displayNumber(action.estimated_savings)}` : "ingen ändring",
        displayNumber(heater?.kwh), displayNumber(heater?.cost), `${item.priced_complete_hours}/${item.total_hours} h`,
      ];
    });
    savingsTable($("savings-daily"), ...withoutEmptyActions(["Dag", "Pump · kWh", `Pump · ${currency}`, `Prisets tidpunkt · ${currency}`, `Före/efter · skillnad · ${currency}`, "Elpatron · kWh", `Elpatron · ${currency}`, "Prisdata"], dailyRows));
    const monthlyRows = data.energy.monthly.filter((item) => hasData(item, monthlyHeater)).map((item) => {
      const heater = monthlyHeater.get(item.period);
      const action = monthlyActions.get(item.period);
      return [
        savingsDate(item.period, true), displayNumber(item.kwh), displayNumber(item.cost), displayNumber(item.price_shift?.difference),
        action ? `${action.estimated_savings > 0 ? "+" : ""}${displayNumber(action.estimated_savings)}` : "ingen ändring",
        displayNumber(heater?.kwh), displayNumber(heater?.cost), `${item.priced_complete_hours}/${item.total_hours} h`,
      ];
    });
    savingsTable($("savings-monthly"), ...withoutEmptyActions(["Månad", "Pump · kWh", `Pump · ${currency}`, `Prisets tidpunkt · ${currency}`, `Före/efter · skillnad · ${currency}`, "Elpatron · kWh", `Elpatron · ${currency}`, "Prisdata"], monthlyRows));
    const actionRows = [...(data.actions.items || [])].reverse()
      .map((item) => {
        const costDirection = item.estimated_savings > 0 ? "lägre kostnad efteråt"
          : item.estimated_savings < 0 ? "högre kostnad efteråt" : "oförändrad kostnad";
        const kwhDirection = item.estimated_kwh_change == null ? "kWh saknas"
          : item.estimated_kwh_change > 0 ? `${displayNumber(Math.abs(item.estimated_kwh_change))} kWh lägre förbrukning efteråt`
            : item.estimated_kwh_change < 0 ? `${displayNumber(Math.abs(item.estimated_kwh_change))} kWh högre förbrukning efteråt`
              : "oförändrad förbrukning";
        const control = item.kind === "offset" ? "Kurvförskjutning" : "Termostat";
        const unit = item.unit || (item.kind === "offset" ? "steg" : "°C");
        const change = `${displayNumber(item.previous)} → ${displayNumber(item.value)} ${unit}`;
        const factors = item.contributions ? Object.entries({
          room: "rum", weather: "väder", price: "pris", fireplace: "kamin", base_correction: "baskorrigering",
        }).filter(([key]) => item.contributions[key] != null && item.contributions[key] !== 0)
          .map(([key, label]) => `${label} ${displayNumber(item.contributions[key])}`).join(", ") : "";
        const context = [
          item.price != null ? `elpris ${displayNumber(item.price)} ${item.price_unit || ""}`.trim() : "",
          item.preheat ? (item.preheat === "preheat" ? "förvärmning" : "dyrpris/sparläge") : "",
          factors ? `styrbidrag (${factors})` : "",
        ].filter(Boolean).join(" · ");
        const li = document.createElement("li");
        li.textContent = `${new Date(item.timestamp * 1000).toLocaleString("sv-SE")} · ${control} ${change}. Före: ${displayNumber(item.before.cost)} ${currency}, ${displayNumber(item.before.kwh)} kWh; efter: ${displayNumber(item.after.cost)} ${currency}, ${displayNumber(item.after.kwh)} kWh — ${displayNumber(Math.abs(item.estimated_savings))} ${currency} ${costDirection}, ${kwhDirection}.${context ? ` Vid beslutet: ${context}.` : ""}`;
        return li;
      });
    $("savings-actions").replaceChildren(...actionRows);
    if (data.action_error) $("savings-actions").textContent = data.action_error;
    else if (!actionRows.length) $("savings-actions").textContent = "Inga bekräftade kurv- eller termostatändringar med kompletta kostnadstimmar att jämföra i perioden.";
    const settingsRows = (data.settings_changes || []).map((item) => {
      const changes = Object.entries(item.changes || {})
        .map(([key, change]) => `${fieldLabels[key] || key}: ${logValue(change.from)} → ${logValue(change.to)}`);
      const li = document.createElement("li");
      li.textContent = `${new Date(item.timestamp * 1000).toLocaleString("sv-SE")} · ${changes.join("; ") || item.message}`;
      return li;
    });
    $("savings-settings").replaceChildren(...settingsRows);
    if (data.settings_error) $("savings-settings").textContent = data.settings_error;
    else if (!settingsRows.length) $("savings-settings").textContent = "Inga sparade inställningsändringar i perioden.";
    const runKeys = data.heater ? contextKeys(data.heater) : [];
    const runRows = (data.heater?.runs || []).map((run) => {
      const context = runContext(run, runKeys);
      return [
        runPeriod(run, data.heater.start ?? data.start),
        duration(run.seconds), `${displayNumber(run.max_kw)} kW`, `${displayNumber(run.kwh)} kWh`,
        `${displayNumber(run.cost)} ${currency}${run.cost === null ? " (saknar fullständigt pris)" : ""}`, context,
      ];
    });
    $("savings-heater-status").textContent = data.heater_error || (data.heater
      ? `${data.heater.run_count} observerade perioder totalt. ${data.heater.runs_truncated ? "Visar de 50 senaste. " : ""}Uppskattad elpatronkostnad ingår redan i pumpens kostnad. Korta avbrott i myUplink delar inte upp en körning.`
      : "Elpatronhistorik saknas.");
    savingsTable($("savings-heater-runs"), ["Körperiod", "Drifttid", "Högsta effekt", "El", "Kostnad", "Samtidiga värden · inte säker orsak"], runRows);
    $("savings-data").hidden = false;
    chartRedraws.get($("savings-chart"))?.();
  } catch (error) {
    $("savings-status").textContent = `Kunde inte läsa besparingskalkylen: ${error.message}. Försöker igen vid nästa uppdatering.`;
  } finally {
    savingsBusy = false;
    if (pendingRefreshes.delete("savings")) void refreshSavings();
  }
}

let sourcesBusy = false;

async function refreshSources() {
  if (sourcesBusy) { pendingRefreshes.add("sources"); return; }
  sourcesBusy = true;
  const pill = $("sources-pill");
  try {
    if (!settings?.grid_import_entity) {
      $("sources-data").hidden = true;
      $("sources-status").textContent = "Välj husets elflöden under Inställningar → Husets el för att se andelen sol och batteri.";
      pill.textContent = "Inte vald";
      pill.dataset.tone = "muted";
      return;
    }
    const data = await api("sources");
    const t = data.house_totals;
    const share = (part) => (t.house_kwh > 0 ? Math.round((100 * part) / t.house_kwh) : null);
    $("sources-status").textContent = `${dayAndTime(data.start)} – ${dayAndTime(data.end)}.${data.house_complete ? "" : " Vissa timmar saknar data och räknas inte."}`;
    $("sources-metrics").replaceChildren(
      metric({ label: "Huset använde", value: displayNumber(t.house_kwh), unit: "kWh" }),
      metric({ label: `Från elnätet · ${share(t.grid_kwh) ?? "–"} %`, value: displayNumber(t.grid_kwh), unit: "kWh" }),
      metric({ label: `Direkt från sol · ${share(t.solar_kwh) ?? "–"} %`, value: displayNumber(t.solar_kwh), unit: "kWh" }),
      metric({ label: `Från batteri · ${share(t.battery_kwh) ?? "–"} %`, value: displayNumber(t.battery_kwh), unit: "kWh" }),
    );
    const local = share(t.solar_kwh + t.battery_kwh);
    pill.textContent = local === null ? "Ingen data" : `${local} % sol och batteri`;
    pill.dataset.tone = local >= 50 ? "green" : local > 0 ? "amber" : "muted";
    drawChart($("sources-chart"), [
      { name: "Elnät", points: data.house.map((h) => [h.start, h.grid_kwh]), until: data.end },
      { name: "Sol", points: data.house.map((h) => [h.start, h.solar_kwh]), until: data.end },
      { name: "Batteri", points: data.house.map((h) => [h.start, h.battery_kwh]), until: data.end },
    ], { start: data.start, end: data.end, unit: " kWh", integer: false, pad: 0.1, left: 96,
      empty: "Inga fullständiga timvärden.", label: "Husets el per timme" });
    legend($("sources-legend"), [{ name: "Elnät" }, { name: "Sol" }, { name: "Batteri" }]);
    const p = data.pump_totals;
    $("sources-pump").textContent = p
      ? `Pumpen · uppskattat: ${displayNumber(p.kwh)} kWh, varav ${displayNumber(p.solar_kwh)} från sol, ${displayNumber(p.battery_kwh)} från batteri och ${displayNumber(p.grid_kwh)} från elnätet.${p.grid_cost != null ? ` Uppskattat elpris för nätandel ${displayNumber(p.grid_cost)} ${data.currency || ""}; teoretiskt värde för lokal andel ${displayNumber(p.avoided_cost)} ${data.currency || ""}, före exempelvis ersättning för såld el.` : ""}${p.complete ? "" : unallocatedText(data)}`
      : "Pumpens andel visas när en elmätare för pumpen är vald under Inställningar → Din värmepump.";
    $("sources-pump").title = p ? "" : data.pump_error || "";
    $("sources-data").hidden = false;
  } catch (error) {
    $("sources-data").hidden = true;
    $("sources-status").textContent = `Kunde inte räkna husets elflöden: ${error.message}`;
    pill.textContent = "Fel";
    pill.dataset.tone = "red";
  } finally {
    sourcesBusy = false;
    if (pendingRefreshes.delete("sources")) void refreshSources();
  }
}

async function refreshAuxiliary() {
  if (auxiliaryBusy) { pendingRefreshes.add("auxiliary"); return; }
  auxiliaryBusy = true;
  const signature = () => JSON.stringify([settings.offset_entity, settings.price_entity, settings.price_provider, settings.max_age_minutes]);
  const requested = signature();
  $("auxiliary-data").hidden = true;
  for (const id of ["auxiliary-metrics", "auxiliary-chart", "auxiliary-runs"]) {
    chartRedraws.delete($(id));
    $(id).replaceChildren();
  }
  try {
    $("auxiliary-status").textContent = "Hämtar elpatronens historik…";
    $("auxiliary-section").classList.remove("is-empty");
    const data = await api("auxiliary");
    if (signature() !== requested) { pendingRefreshes.add("auxiliary"); return; }
    const totals = data.totals;
    const covered = totals.coverage_seconds / 3600;
    $("auxiliary-status").textContent = `${periodText(data.start, data.end)}. Elpatronens effekt enligt pumpen.${bridgedText(data.bridged_seconds)}`;
    $("auxiliary-status").title = data.meter.entity_id;
    $("auxiliary-current").textContent = data.current.error
      ? `Just nu: okänt. ${data.current.error}`
      : `Senast avläst ${dayAndTime(data.checked_at)}: elpatronen är ${data.current.active ? "i drift" : "av"} · ${displayNumber(data.current.kw)} kW.`;
    $("auxiliary-metrics").replaceChildren(
      metric({ label: "Observerade körperioder · täckt del", value: covered > 0 ? data.runs.length : null, unit: "st" }),
      metric({ label: "Observerad drifttid · täckt del", value: covered > 0 ? duration(data.runtime_seconds) : null }),
      metric({ label: "Uppskattad el · täckt del", value: covered > 0 ? displayNumber(totals.measured_kwh) : null, unit: "kWh" }),
      metric({ label: "Uppskattad kostnad · prissatt del", value: totals.priced_seconds > 0 ? displayNumber(totals.partial_cost) : null, unit: data.currency || "" }),
    );
    $("auxiliary-coverage").textContent = `Datatäckning ${displayNumber(covered)} av 24 timmar. ${data.observed_starts} ${data.observed_starts === 1 ? "start" : "starter"} med observerad övergång från av till på. ${data.skipped_intervals} intervall med lucka eller ogiltiga värden utelämnade. Perioder kan ha börjat före eller fortsatt efter den täckta delen.`;
    $("auxiliary-price-status").title = data.price_entity || "";
    $("auxiliary-price-status").textContent = `${data.price_entity ? (data.price_provider === "nordpool" ? "Nord Pool" : "Tibber") : "Ingen sparad prisgivare"}. Prissatt datatäckning ${displayNumber(totals.priced_seconds / 3600)} av 24 timmar. ${data.price_error || "Endast avgifter och moms som finns i prisgivaren ingår."}`;
    drawChart($("auxiliary-chart"), [{
      name: "Elpatronens uppskattade el", points: data.hours.map((hour) => [hour.start, hour.kwh]), until: data.end,
    }], { start: data.start, end: data.end, unit: " kWh", integer: false, left: 96, pad: 0.1,
      label: "Elpatronens uppskattade el per timme", empty: "Inga fullständiga timvärden. Se datatäckningen." });
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    for (const label of ["Period", "Drifttid", "Högsta effekt", "Uppskattad el", "Uppskattad kostnad", "Samtidigt vid början · inte säker orsak"]) {
      const th = document.createElement("th");
      th.textContent = label;
      head.append(th);
    }
    const body = table.createTBody();
    const keys = contextKeys(data);
    for (const run of [...data.runs].reverse()) {
      const context = runContext(run, keys);
      const row = body.insertRow();
      for (const value of [
        runPeriod(run, data.start),
        duration(run.seconds), `${displayNumber(run.max_kw)} kW`, `${displayNumber(run.kwh)} kWh`,
        `${displayNumber(run.cost)} ${data.currency || ""}${run.cost === null ? ` (prissatt del ${run.priced_seconds > 0 ? displayNumber(run.partial_cost) : "—"})` : ""}`,
        context,
      ]) row.insertCell().textContent = value;
    }
    if (data.runs.length) $("auxiliary-runs").append(table);
    else $("auxiliary-runs").textContent = covered > 0
      ? "Inga körperioder observerades i den täckta delen. Det säger inget om dataluckorna."
      : "Underlag saknas. Inga slutsatser om drifttid eller kostnad kan dras.";
    $("auxiliary-data").hidden = false;
    // The chart was drawn while hidden, so redraw it at its real width.
    chartRedraws.get($("auxiliary-chart"))?.();
    featureTone("auxiliary-section", "auxiliary-pill", data.current.error ? "muted" : data.current.active ? "amber" : "green",
      data.current.error ? "Okänt just nu" : data.current.active ? `I drift · ${displayNumber(data.current.kw)} kW` : "Av just nu");
  } catch (error) {
    if (signature() !== requested) { pendingRefreshes.add("auxiliary"); return; }
    $("auxiliary-data").hidden = true;
    $("auxiliary-status").textContent = `Kunde inte läsa elpatronen: ${error.message.replace(/[.!?]+\s*$/, "")}. Försöker igen automatiskt.`;
    $("auxiliary-section").classList.add("is-empty");
    featureTone("auxiliary-section", "auxiliary-pill", "muted", "Ingen data");
  } finally {
    auxiliaryBusy = false;
    if (pendingRefreshes.delete("auxiliary")) void refreshAuxiliary(); void refreshSources();
  }
}

function operationText(value) {
  const text = typeof value === "string" ? value.replace(/\u00ad/g, "") : value;
  return ({ Heating: "Värme", "Hot water": "Varmvatten", Idle: "Vilar", Off: "Av", Operating: "I drift", Blocked: "Blockerad",
    Transfer: "Överföring", Cooling: "Kyla" })[text] || text;
}

const RUN_CONTEXT = { operation: "Driftläge", outdoor: "Ute", degree_minutes: "Gradminuter", hot_water: "Varmvatten" };

function contextValue(point) {
  const number = Number(point.value);
  return point.value !== "" && Number.isFinite(number) ? `${oneDecimal(number)} ${point.unit || ""}`.trim() : operationText(point.value);
}

// Only values this pump reports: one missing at a single start is "okänt", one the pump never reports is left out.
function contextKeys(data) {
  return Object.keys(RUN_CONTEXT).filter((key) => (data.context_sources && Object.keys(data.context_sources).length)
    ? key in data.context_sources : (data.runs || []).some((run) => run.context?.[key]));
}

function runContext(run, keys) {
  return keys.map((key) => `${RUN_CONTEXT[key]}: ${run.context?.[key] ? contextValue(run.context[key]) : "okänt"}`).join(" · ") || "—";
}

function runPeriod(run, periodStart) {
  const notes = [];
  if (!run.start_observed) notes.push(run.start <= periodStart ? "började före perioden" : "exakt start okänd, lucka i myUplink-datan");
  if (run.continues) notes.push("fortsatte efter periodens slut");
  else if (!run.end_observed) notes.push("exakt stopp okänt, lucka i myUplink-datan");
  return `${periodText(run.start, run.end)}${notes.length ? ` (${notes.join("; ")})` : ""}`;
}

function renderHistory() {
  const data = historyData;
  const lines = data.series.filter((line) => !line.outdoor);
  const select = $("history-entity");
  const chosen = select.value;
  fillSelect(select, [new Option("Alla rum", ""), ...lines.map((line) => new Option(line.name, line.entity_id))],
    lines.some((line) => line.entity_id === chosen) ? chosen : "");
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
  $("history-area-status").textContent = areaError
    ? `Home Assistant-rummens namn kunde inte läsas: ${areaError}. Historiken visas i en gemensam grupp.`
    : "Varje kort följer rumstilldelningen i Home Assistant. Kurvändringar påverkar hela huset och räknas inte som en ändring i ett enskilt rum.";
  $("thermostat-charts").replaceChildren();
  const roomGroups = groupRooms(single ? [single] : lines, (line) => line.entity_id);
  for (const { name, rooms: members } of roomGroups.values()) {
    const card = document.createElement("section");
    card.className = "thermostat-history";
    const title = document.createElement("h3");
    title.textContent = name;
    const summary = document.createElement("p");
    summary.className = "room-history-summary";
    summary.textContent = members.map((line) => {
      const readings = line.points.filter((point) => point[1] !== null);
      if (!readings.length) return `${line.name}: ingen temperaturhistorik`;
      const first = readings[0][1], current = readings.at(-1)[1];
      const change = current - first;
      return `${line.name}: ${oneDecimal(current)} °C nu · ${change >= 0 ? "+" : ""}${oneDecimal(change)} °C under dygnet`;
    }).join(" · ");
    const chart = document.createElement("div");
    chart.className = "chart";
    chart.setAttribute("role", "img");
    chart.setAttribute("aria-label", `${name}: rumstemperaturer och termostatbörvärden senaste dygnet`);
    const roomSeries = members.flatMap((line, index) => [
      { name: `${line.name} · uppmätt °C`, points: line.points, until: data.end, color: index },
      ...(line.kind === "climate" ? [{
        name: `${line.name} · termostat °C`, points: line.setpoints || [], until: data.end, color: index, dashed: true,
      }] : []),
    ]);
    const roomLegend = document.createElement("div");
    roomLegend.className = "chart-legend";
    const roomChanges = (data.changes || []).filter((change) => change.kind === "thermostat"
      && members.some((line) => line.entity_id === change.entity_id));
    const changeList = document.createElement("ul");
    changeList.className = "room-history-changes";
    if (roomChanges.length) {
      for (const change of roomChanges) {
        const item = document.createElement("li");
        item.className = change.status === "confirmed" ? "done-auto" : "done-other";
        item.textContent = `${clock(change.timestamp)} · ${entities.find((entity) => entity.entity_id === change.entity_id)?.name || change.entity_id}: ${historyChangeText(change)}`;
        changeList.append(item);
      }
    } else {
      const item = document.createElement("li");
      item.textContent = "Inga termostatändringar loggade i detta rum under dygnet.";
      changeList.append(item);
    }
    card.append(title, summary, chart, roomLegend, changeList);
    $("thermostat-charts").append(card);
    legend(roomLegend, roomSeries);
    drawChart(chart, roomSeries, { start: data.start, end: data.end, guides: members.length === 1 && members[0].kind === "sensor"
      ? [{ value: data.target, label: `mål ${displayNumber(data.target)}°` }] : [],
    markers: historyMarkers(roomChanges) });
  }
  window.requestAnimationFrame(() => {
    for (const chart of $("thermostat-charts").querySelectorAll(".chart")) chartRedraws.get(chart)?.();
  });
  $("thermostat-charts-title").hidden = roomGroups.size === 0;
  const last = (points) => [...(points || [])].reverse().find((point) => point[1] !== null)?.[1] ?? null;
  const count = lines.reduce((sum, line) => sum + line.points.filter((point) => point[1] !== null).length, 0);
  let status = count
    ? `${lines.length} rumsgivare · senaste 24 timmarna · uppdaterad ${new Date(data.end * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}`
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
  if (historyBusy) { pendingRefreshes.add("history"); return; }
  historyBusy = true;
  $("refresh-history").disabled = true;
  try {
    await refreshAreas();
    historyData = await api("history");
    renderHistory();
    renderRoomImpact();
  } catch (error) {
    historyData = null;
    renderRoomImpact();
    renderHomeHistory();
    $("temperature-chart").replaceChildren();
    $("temperature-legend").replaceChildren();
    $("history-table").replaceChildren();
    $("history-changes").replaceChildren();
    $("thermostat-charts").replaceChildren();
    $("thermostat-charts-title").hidden = true;
    $("history-changes-status").textContent = "Ändringshistoriken är inte tillgänglig.";
    $("history-status").textContent = `Kunde inte hämta historik: ${error.message}`;
  } finally {
    historyBusy = false; $("refresh-history").disabled = false;
    if (pendingRefreshes.delete("history")) refreshHistory();
  }
}

async function refreshAreas() {
  try {
    entityAreas = await api("areas");
    areaError = "";
  } catch (error) {
    entityAreas = {};
    areaError = error.message;
  }
  renderRoomPickerAreas();
  if (lastStatus) renderRoomImpact();
  if (historyData) renderHistory();
}

async function refreshForecast() {
  try {
    const data = await api("forecast");
    const points = data.points.slice(0, data.type === "hourly" ? 25 : 7);
    const start = points[0].timestamp;
    const end = Math.max(start + 3600, points.at(-1).timestamp);
    drawChart($("forecast-chart"), [{ name: "Prognos", points: points.map((point) => [point.timestamp, point.temperature]), step: false }], { start, end });
    $("forecast-status").textContent = `${data.name} · ${data.type === "hourly" ? "timprognos" : "dygnsprognos"} · hämtad ${new Date(data.fetched_at * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}`;
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
  if (planBusy) { pendingRefreshes.add("plan"); return; }
  planBusy = true;
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

    const prices = [{ name: `Elpris${data.prices.unit ? ` · ${data.prices.unit}` : ""}`, points: data.prices.points, until: data.prices.end ?? data.end, color: 1 }];
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
    $("plan-status").textContent = `Uppdaterad ${new Date(data.now * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}`;
  } catch (error) {
    for (const id of ["plan-outdoor", "plan-price", "plan-offset", "plan-curve", "plan-changes"]) $(id).replaceChildren();
    $("plan-status").textContent = `Kunde inte hämta värmeplanen: ${error.message}`;
  } finally {
    planBusy = false; $("refresh-plan").disabled = false;
    if (pendingRefreshes.delete("plan")) refreshPlan();
  }
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
  // Text over the coloured water is white; over the empty part it stays dark.
  const onFill = (y) => (level !== null && y - 6 > inner.y + inner.h * (1 - level) ? " on-fill" : "");
  const clip = svgElement("clipPath", { id: "hw-clip" });
  clip.append(svgElement("rect", { x: inner.x, y: inner.y, width: inner.w, height: inner.h, rx: 20 }));
  svg.append(clip);
  svg.append(svgElement("rect", { x: inner.x, y: inner.y, width: inner.w, height: inner.h, rx: 20, class: "hw-empty" }));
  if (level !== null) {
    const fill = inner.h * level;
    const tone = level >= 0.5 ? "hw-hot" : level >= 0.25 ? "hw-warm" : "hw-cold";
    svg.append(svgElement("rect", { x: inner.x, y: inner.y + inner.h - fill, width: inner.w, height: fill, class: `hw-fill ${tone}`, "clip-path": "url(#hw-clip)" }));
    svg.append(svgElement("text", { x: width / 2, y: inner.y + inner.h / 2 + 8, "text-anchor": "middle", class: `hw-percent${onFill(inner.y + inner.h / 2 + 8)}` }, `${Math.round(level * 100)}%`));
  } else {
    svg.append(svgElement("text", { x: width / 2, y: inner.y + inner.h / 2 + 6, "text-anchor": "middle", class: "hw-percent small" }, "okänt"));
  }
  if (typeof data.top === "number") svg.append(svgElement("text", { x: width / 2, y: inner.y + 22, "text-anchor": "middle", class: `hw-label${onFill(inner.y + 22)}` }, `topp ${displayNumber(data.top)}°`));
  if (typeof data.charge === "number") svg.append(svgElement("text", { x: width / 2, y: inner.y + inner.h - 14, "text-anchor": "middle", class: `hw-label${onFill(inner.y + inner.h - 14)}` }, `BT6 ${displayNumber(data.charge)}°`));
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

let hotWaterMonitorBusy = false;
let hotWaterMonitorPending = false;
async function refreshHotWaterMonitor() {
  if (!$("hot-water-card").getClientRects().length) return;
  if (hotWaterMonitorBusy) { hotWaterMonitorPending = true; return; }
  hotWaterMonitorBusy = true;
  try {
    const hours = $("hw-monitor-hours").value;
    const data = await api(`hot-water-monitor?hours=${encodeURIComponent(hours)}`);
    $("hw-monitor-control").textContent = data.price_control_enabled
      ? "Varmvatten-prisstyrning är PÅ. Pumpens läge kan ändras av appen."
      : "Varmvatten-prisstyrning är AV. Pumpen behåller sitt aktuella läge och sköter varmvattnet.";
    const current = {
      charging: "Pumpen rapporterar varmvattenladdning.",
      idle: "Pumpen rapporterar ingen varmvattenladdning.",
      unknown: "Aktuell varmvattenstatus är okänd.",
    }[data.current];
    const reported = data.current_reported_at ? ` Statusgivaren rapporterade senast ${new Date(data.current_reported_at).toLocaleString("sv-SE")}; provtagning innebär inte en ny pumprapport.` : "";
    $("hw-monitor-status").textContent = `${current} ${data.message} ${data.sample_count} prover${data.last_sample ? `; senaste ${dayAndTime(data.last_sample)}` : ""}.${reported}${data.truncated ? " Visar högst 100 händelser av varje typ." : ""}`;
    const charges = data.charges.map((charge) => {
      const article = document.createElement("article");
      article.className = "event";
      const heading = document.createElement("strong");
      heading.textContent = charge.end ? "Varmvattenladdning avslutad"
        : charge.interrupted ? "Laddning avbruten i loggen" : "Varmvattenladdning pågår eller väntar på nytt prov";
      const time = document.createElement("small");
      const duration = Math.floor(charge.observed_seconds / 60);
      time.textContent = `Först avläst ${dayAndTime(charge.start)}${charge.end ? ` · stopp avläst ${dayAndTime(charge.end)}` : ""} · observerat minst ${duration} min${charge.start_known ? "" : " · start före första säkra provet är möjlig"}`;
      const outcome = document.createElement("p");
      outcome.textContent = {
        observed: "Larm observerat under laddningen eller uppföljningen efteråt. Kontrollera larmvyn och pumpen.",
        none_observed: "Inget aktivt larm observerat under laddningen eller de följande tio minuterna. Det bevisar inte att inga händelser missades.",
        unknown: "Larmresultat okänt: laddningen, efterföljande tio minuter eller larmstatus har inte observerats sammanhängande.",
      }[charge.alarm_result];
      const temperatures = document.createElement("p");
      temperatures.className = "hint";
      const start = charge.start_values || {}, end = charge.end_values || {};
      const fields = [["bt6", "Tank BT6"], ["bt7", "Topp BT7"], ["bt2", "Fram BT2"], ["bt3", "Retur BT3"]];
      const readings = fields.flatMap(([key, label]) => {
        if (typeof start[key] !== "number" && typeof end[key] !== "number") return [];
        const from = typeof start[key] === "number" ? `${displayNumber(start[key])}°` : "—";
        const to = typeof end[key] === "number" ? `${displayNumber(end[key])}°` : "—";
        return [`${label}: ${from} → ${to}`];
      });
      if (start.mode) readings.unshift(`Varmvattenläge vid start: ${start.mode}`);
      temperatures.textContent = readings.length ? readings.join(" · ") : "Temperatur eller varmvattenläge saknas i start-/stopproverna.";
      article.append(time, heading, outcome, temperatures);
      return article;
    });
    $("hw-monitor-charges").replaceChildren(...charges);
    if (!charges.length) $("hw-monitor-charges").textContent = "Ingen varmvattenladdning har fångats i perioden.";
    const offsets = data.offset_changes.map((change) => {
      const article = document.createElement("article");
      article.className = "event";
      const time = document.createElement("small");
      time.textContent = dayAndTime(change.timestamp);
      const text = document.createElement("strong");
      text.textContent = `Förskjutning ${displayNumber(change.previous)} → ${displayNumber(change.value)}`;
      const outcome = document.createElement("p");
      outcome.textContent = {
        observed: "Larm observerat under uppföljningen efter ändringen.",
        none_observed: "Inget aktivt larm observerat under de följande tio minuterna; senare larm kan fortfarande uppstå.",
        unknown: "Larmresultat okänt: väntar på tio minuters sammanhängande larmstatus.",
      }[change.alarm_result];
      article.append(time, text, outcome);
      return article;
    });
    $("hw-monitor-offsets").replaceChildren(...offsets);
    if (!offsets.length) $("hw-monitor-offsets").textContent = "Ingen ändring av förskjutningen har avlästs i perioden.";
  } catch (error) {
    $("hw-monitor-status").textContent = `Kunde inte läsa övervakningen: ${error.message}`;
  } finally {
    hotWaterMonitorBusy = false;
    if (hotWaterMonitorPending) {
      hotWaterMonitorPending = false;
      void refreshHotWaterMonitor();
    }
  }
}

async function refreshHotWater() {
  if (hotWaterBusy) { pendingRefreshes.add("hot-water"); return; }
  hotWaterBusy = true;
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
    $("hw-status").textContent = `Uppdaterad ${new Date(data.now * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}. Bedömningen bygger på pumpens givare och är ungefärlig.`;
  } catch (error) {
    $("hw-status").textContent = `Kunde inte läsa varmvattnet: ${error.message}`;
  } finally {
    hotWaterBusy = false; $("refresh-hot-water").disabled = false;
    if (pendingRefreshes.delete("hot-water")) refreshHotWater();
  }
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
  if (learningBusy) { pendingRefreshes.add("learning"); return; }
  learningBusy = true;
  $("refresh-learning").disabled = true;
  try {
    const data = await api("learning");
    const model = data.model;
    const titles = { learning: "Lär sig fortfarande", rough: "Preliminär husmodell", good: "Känner huset väl" };
    $("learn-status").textContent = data.error || (model ? `${titles[model.status]} · uppdaterad ${new Date(data.now * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}` : "Ingen modell ännu.");
    $("learn-message").textContent = [model?.message, data.prediction_note].filter(Boolean).join(" ");
    featureTone("learning-card", "learn-pill", { learning: "blue", rough: "amber", good: "green" }[model?.status] || "muted",
      { learning: "Lär sig", rough: "Preliminär", good: "Känner huset" }[model?.status] || "Ingen modell");
    $("learn-meter-text").textContent = model ? (model.days >= 7 ? `Klart · ${displayNumber(model.days)} dygn` : `${displayNumber(model.days)} av 7 dygn`) : "–";
    $("learn-meter-bar").style.width = `${model ? Math.round(Math.max(0, Math.min(1, model.hours / model.needed_hours)) * 100) : 0}%`;
    $("learn-message").hidden = !$("learn-message").textContent;
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
      bands: [
        ...windows.map((w) => ({ start: w.start, end: w.end, className: w.kind === "preheat" ? "preheat" : "coast",
          title: `${w.kind === "preheat" ? "Förvärmning" : "Dyrt elpris"} ${clock(w.start)}–${clock(w.end)}` })),
        ...(data.fireplace_periods || []).map((w) => ({ start: w.start, end: w.end, className: "fireplace",
          title: `Misstänkt extravärme ${clock(w.start)}–${clock(w.end)}, inte bekräftad eldning` })),
      ],
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
    renderPreheatTimeline(windows, data.now);
  } catch (error) {
    $("learn-status").textContent = `Kunde inte läsa husmodellen: ${error.message}`;
    featureTone("learning-card", "learn-pill", "muted", "Kan inte läsas");
    $("learn-meter-text").textContent = "—";
    $("learn-meter-bar").style.width = "0%";
    $("preheat-timeline").replaceChildren();
  } finally {
    learningBusy = false; $("refresh-learning").disabled = false;
    if (pendingRefreshes.delete("learning")) refreshLearning();
  }
}

const fieldLabels = {
  target_temperature: "Måltemperatur", room_sensors: "Rumsgivare", priority_sensors: "Prioriterade rum", enabled: "Automatik",
  nearest_thermostat: "Närmast värmepumpen", thermostat_control_enabled: "Termostatbegränsning",
  fireplace_enabled: "Kaminanpassning", fireplace_rooms: "Kaminrum", fireplace_max_age_minutes: "Kaminens mätvärdesålder",
  offset_entity: "Pumpanslutning", outdoor_entity: "Utegivare", price_entity: "Elprisgivare", price_provider: "Elpriskälla",
  forecast_entity: "Prognoskälla", energy_entity: "Pumpens elmätare", house_power_entity: "Husets förbrukning (kalibrering)", base_offset: "Basförskjutning", min_offset: "Lägsta förskjutning",
  max_offset: "Högsta förskjutning", weather_gain: "Väderpåverkan", room_gain: "Rumspåverkan",
  price_low: "Lågprisgräns", price_high: "Högprisgräns", cheap_boost: "Höjning vid lågt pris",
  expensive_reduction: "Sänkning vid högt pris", deadband: "Toleransband",
  skip_silent_rooms: "Styr vidare vid tyst givare", price_mode: "Prisnivå", price_low_percent: "Billigt (andel av dygnet)",
  price_high_percent: "Dyrt (andel av dygnet)", auto_base_enabled: "Automatisk baskorrigering",
  auto_base_limit: "Gräns för baskorrigering", publish_sensors: "Sensorer i Home Assistant", share_usage: "Anonym användningsstatistik",
  pause_notify_minutes: "Meddela vid paus efter", notify_service: "Notify-tjänst",
  hot_water_price_enabled: "Varmvatten på billig el", hot_water_mode_entity: "Varmvattenläge",
  hot_water_cheap_option: "Läge vid billig el", hot_water_normal_option: "Vanligt varmvattenläge",
  aux_limit_enabled: "Begränsa elpatronen vid dyr el", aux_limit_entity: "Max eltillsats-inställning",
  aux_limit_option: "Max eltillsats vid dyr el", aux_normal_option: "Vanlig max eltillsats",
  aux_limit_max_drop: "Släpp begränsning under målet", aux_limit_min_outdoor: "Ingen begränsning under utetemp",
  aux_hot_water_guard: "Varmvattenvakt", aux_hot_water_min_charge: "Varmvattenvakt · BT6-gräns", aux_hot_water_min_top: "Varmvattenvakt · BT7-gräns",
  hot_water_precharge_minutes: "Ladda före dyra timmar",
  grid_import_entity: "Köpt el", grid_export_entity: "Såld el", solar_entity: "Solproduktion",
  battery_discharge_entity: "Batteri ur", battery_charge_entity: "Batteri in",
  min_interval_minutes: "Minsta ändringsintervall", max_step: "Största steg", max_age_minutes: "Högsta mätvärdesålder",
  preheat_enabled: "Förvärmning", preheat_degrees: "Förvärmning · grader",
  adaptive_reduction_mode: "Behovsanpassad sänkning", auto_base_memory_days: "Baskorrigeringens minne",
  solar_mode: "Solvärme", solar_power_entity: "Solcellseffekt", solar_power_threshold: "Solen räknas från",
  hot_water_habit_mode: "Varmvatten efter vanor",
  radiator_mode: "Radiatortermostater", radiator_pairs: "Radiatortermostater och rumsgivare",
  radiator_close_above: "Stryp radiator vid", radiator_open_above: "Öppna radiator vid",
  radiator_closed_setpoint: "Börvärde vid strypning", radiator_close_minutes: "Varmt minst (radiator)",
};

function logValue(value) {
  if (typeof value === "boolean") return value ? "på" : "av";
  if (Array.isArray(value)) return value.join(", ") || "inga";
  return String(value ?? "saknas");
}

function renderEvents() {
  const openIds = new Set([...$("events-list").querySelectorAll(".event details[open]")].map((details) => details.closest(".event").dataset.eventId));
  replaceIfChanged($("events-list"), eventRows.map((event) => {
    const entry = document.createElement("article");
    entry.className = "event";
    entry.dataset.eventId = String(event.id);
    const heading = document.createElement("strong");
    heading.textContent = event.message;
    const stamp = document.createElement("small");
    stamp.textContent = `${new Date(event.timestamp * 1000).toLocaleString("sv-SE")} · #${event.id}`;
    entry.append(stamp, heading);
    const data = event.details;
    const lines = [];
    for (const [key, change] of Object.entries(data.changes || {})) lines.push(`${fieldLabels[key] || key}: ${logValue(change.from)} → ${logValue(change.to)}`);
    if (event.kind === "write_attempt") lines.push(`Förskjutning ${data.previous} → ${data.value}. Rum ${displayNumber(data.indoor)} °C${data.indoor_smoothed != null ? ` (timmedel ${displayNumber(data.indoor_smoothed)} °C)` : ""}, mål ${displayNumber(data.target_temperature)} °C. Rumspåverkan ${displayNumber(data.contributions.room)}, väder ${displayNumber(data.contributions.weather)}, pris ${displayNumber(data.contributions.price)}.${data.adaptive_reduction != null ? ` Behovsanpassad sänkning ${displayNumber(data.adaptive_reduction)} steg.` : ""}${data.target_shift ? ` Mål tillfälligt ${data.target_shift > 0 ? "+" : ""}${displayNumber(data.target_shift)}° (${[data.preheat === "preheat" ? "förvärmning" : data.preheat === "coast" ? "dyrt elpris" : "", data.solar ? "solvärme" : ""].filter(Boolean).join(", ")}).` : ""}`);
    if (data.requested !== undefined) lines.push(`Begärt: ${data.requested}. Återläst: ${data.observed}.`);
    if (event.kind === "test_write") lines.push(`Förskjutning ${data.previous} → ${data.value} i ${data.restore_after_minutes} minuter.`);
    if (event.kind === "test_restore") lines.push(`Tillbaka till ${data.value}.`);
    if (event.kind.startsWith("thermostat_")) {
      if (data.entity_id) lines.push(`Termostat: ${data.entity_id}.`);
      if (data.value !== undefined) lines.push(`Börvärde: ${data.previous !== undefined ? `${data.previous} → ` : ""}${data.value}${data.unit ? ` ${data.unit}` : ""}.`);
    }
    if (event.kind === "offset_observed") lines.push(`${data.previous} → ${data.value}. ${data.note}`);
    if (event.kind.startsWith("fireplace_")) lines.push(`Kaminrum: ${(data.rooms || []).join(", ")}. Misstänkt extravärme, inte bekräftad eldning.`);
    if (event.kind === "write_attempt" && data.fireplace === "active") lines.push(`Misstänkt extravärme pågick. Extra kaminbidrag ${displayNumber(data.contributions?.fireplace ?? 0)} kurvsteg; prioriterat snitt och vanliga skydd användes.`);
    if (data.error) lines.push(data.error);
    if (data.request_id) lines.push(`Tillhör begäran #${data.request_id}.`);
    if (lines.length) { const p = document.createElement("p"); p.textContent = lines.join("\n"); entry.append(p); }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Tekniska detaljer";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data, null, 2);
    details.append(summary, pre);
    details.open = openIds.has(String(event.id));
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
  if (dashboardBusy) { pendingRefreshes.add("dashboard"); return; }
  dashboardBusy = true;
  try { await Promise.all([refreshOverview(), refreshEvents()]); }
  finally {
    dashboardBusy = false;
    if (pendingRefreshes.delete("dashboard")) void refreshDashboard();
  }
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
  let community = null;
  try {
    community = (await api("community")).community;
  } catch {
    community = null;
  }
  const text = communityText(community);
  $("community").textContent = text ? ` · ${text}` : "";
  const badge = $("community-badge");
  badge.hidden = !community;
  if (community) {
    const n = community.installations;
    badge.textContent = `≈ ${n.toLocaleString("sv-SE")} ${n === 1 ? "installation" : "installationer"}`;
    badge.title = `Aktiva installationer senaste 7 dagarna (${community.total.toLocaleString("sv-SE")} sedan start), räknade från den anonyma användningsstatistiken. Installationer som stängt av den räknas inte. Stängs av under Inställningar → Home Assistant.`;
  }
}

// Installations active in the last 7 days, counted from the anonymous pings (see community.py).
function communityText(community) {
  if (!community) return "";
  const n = community.installations;
  return `≈ ${n} ${n === 1 ? "aktiv installation" : "aktiva installationer"} senaste veckan`;
}

const GROUPED_VIEWS = new Set(["overview", "heat", "water", "energy", "system", "settings"]);

function setPageLayout(mode, view = $("main").dataset.activeView) {
  const main = $("main");
  const selected = GROUPED_VIEWS.has(view) ? view : "overview";
  main.dataset.layout = mode;
  main.dataset.activeView = selected;
  $("group-nav").hidden = mode !== "grouped";
  $("classic-nav").hidden = mode !== "classic";
  document.querySelectorAll(".group-tab").forEach((button) => {
    button.setAttribute("aria-pressed", String(mode === "grouped" && button.dataset.viewTarget === selected));
  });
  try {
    localStorage.setItem("nibe-comfort-layout-mode", mode);
    localStorage.setItem("nibe-comfort-layout-view", selected);
  } catch { /* storage blocked */ }
  window.requestAnimationFrame(() => {
    for (const chart of main.querySelectorAll(".chart")) {
      if (chart.getClientRects().length && chart.querySelector("svg")) chartRedraws.get(chart)?.();
    }
    if ($("hot-water-card").getClientRects().length) void refreshHotWaterMonitor();
  });
}

function initializePageLayout() {
  if (pageLayoutInitialized) return;
  pageLayoutInitialized = true;
  let mode = "grouped", view = "overview";
  try {
    if (localStorage.getItem("nibe-comfort-layout-mode") === "classic") mode = "classic";
    const savedView = localStorage.getItem("nibe-comfort-layout-view");
    if (GROUPED_VIEWS.has(savedView)) view = savedView;
  } catch { /* storage blocked */ }
  setPageLayout(mode, view);
  document.querySelectorAll(".group-tab").forEach((button) => {
    button.addEventListener("click", () => setPageLayout("grouped", button.dataset.viewTarget));
  });
  $("classic-view-toggle").addEventListener("click", () => setPageLayout("classic"));
  $("group-view-toggle").addEventListener("click", () => setPageLayout("grouped"));
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.('a[href^="#"]');
    if (!link) return;
    const target = document.getElementById(decodeURIComponent(link.hash.slice(1)));
    if (!target) return;
    const viewOwner = target.closest("[data-view]");
    if ($("main").dataset.layout === "grouped" && viewOwner) {
      setPageLayout("grouped", viewOwner.dataset.view);
    }
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
  });
}

async function init() {
  initializePageLayout();
  makeAdvanced();
  $("adjust-offset-limits").onclick = () => {
    const view = $("offset-limits").closest("[data-view]")?.dataset.view;
    if ($("main").dataset.layout === "grouped" && view) setPageLayout("grouped", view);
    $("settings-panel").open = true;
    const id = lastStatus?.control_limit?.direction === "lower" ? "min_offset" : "max_offset";
    $(id).focus({ preventScroll: true });
  };
  $("save").disabled = $("save-near").disabled = $("pause").disabled = true;
  try {
    [settings, entities] = await Promise.all([api("settings"), api("entities")]);
    entitiesReadAt = Date.now();
    entities.sort((a, b) => a.name.localeCompare(b.name, "sv"));
    const saved = structuredClone(settings);
    await findPump();
    const discovered = settings;
    settings = saved;
    renderSettings(discovered);
    await Promise.all([refreshAreas(), refreshNotifyServices()]);
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
    if (next.fireplace_enabled && (!settings.fireplace_enabled
      || JSON.stringify(next.fireplace_rooms) !== JSON.stringify(settings.fireplace_rooms)
      || JSON.stringify(next.room_sensors) !== JSON.stringify(settings.room_sensors))
      && !window.confirm("Automatisk temperaturanalys kan misstänka eldning men också reagera på sol eller annan värme. När huvudautomatiken är på får detta sänka den beräknade kurvförskjutningen ett extra steg om det styrande snittet inte är för kallt. Hela huset påverkas och övriga rum kan bli kallare. Godkänner du kaminanpassningen för dessa rum?")) return;
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
    if (next.radiator_mode === "on" && (settings.radiator_mode !== "on"
      || JSON.stringify(next.radiator_pairs) !== JSON.stringify(settings.radiator_pairs))
      && !window.confirm("Valda radiatortermostaters börvärden sänks när rumsgivaren visar att rummet är varmt, och återställs när det svalnat. Ventilerna stryper då flödet; säkerställ att värmesystemet har tillräckligt flöde genom övriga element. Godkänner du detta?")) return;
    if (next.enabled && JSON.stringify(next.priority_sensors) !== JSON.stringify(settings.priority_sensors)
      && !window.confirm("Styrningen kommer att följa snittet för de prioriterade rummen, eller alla valda rum om inget prioriteras. Kurvförskjutningen påverkar hela huset och andra rum kan bli varmare. Spara ändringen?")) return;
    if (next.enabled && (!settings.enabled || next.offset_entity !== settings.offset_entity)
      && !window.confirm("Har du verifierat att vald entitet är kurvförskjutning och att gränserna passar din pump? Automatiken får ändra denna entitet.")) return;
    void save(next);
  });
  $("add-radiator-pair").addEventListener("click", () => {
    $("radiator_pairs").append(radiatorPairRow({ thermostat: "", sensor: "" }, radiatorIsTemperature));
    updateSaveState();
  });
  $("settings").addEventListener("input", updateSaveState);
  $("settings").addEventListener("change", updateSaveState);
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
  $("preheat_degrees").addEventListener("input", updateSaveState);
  $("preheat_enabled").addEventListener("change", updateSaveState);
  $("fireplace_enabled").addEventListener("change", updateSaveState);
  $("fireplace_rooms").addEventListener("change", updateSaveState);
  $("fireplace_max_age_minutes").addEventListener("input", updateSaveState);
  $("refresh-overview").addEventListener("click", () => void refreshOverview());
  $("refresh-alarms").addEventListener("click", () => { void refreshAlarms(); void refreshCloudAlarms(); });
  $("refresh-compressor-diagnostics").addEventListener("click", () => void refreshCompressorDiagnostics());
  $("compressor-diagnostics-hours").addEventListener("change", () => void refreshCompressorDiagnostics());
  $("compressor-parameter").addEventListener("change", () => {
    compressorParameterSelection = $("compressor-parameter").value;
    if (compressorDiagnosticsData) renderCompressorDiagnostics(compressorDiagnosticsData);
  });
  $("alarm-pause").addEventListener("click", () => void pauseAlarmThermostats());
  $("aux-recovery-ack").addEventListener("click", () => void acknowledgeAuxRecovery());
  $("fireplace-manual-start").addEventListener("click", () => void manualFireplace());
  $("notify-test").addEventListener("click", () => void testNotify());
  $("hot_water_mode_entity").addEventListener("change", () => pickerChanged("hot_water"));
  $("aux_limit_entity").addEventListener("change", () => pickerChanged("aux"));
  $("hot-water-suggestion").addEventListener("click", () => useModeSuggestion("hot_water"));
  $("aux-suggestion").addEventListener("click", () => useModeSuggestion("aux"));
  $("notify_service").addEventListener("change", () => { $("notify-test").disabled = !$("notify_service").value; $("notify-status").textContent = ""; });
  $("discover-cloud-devices").addEventListener("click", () => void discoverCloudDevices());
  $("save-cloud-device").addEventListener("click", () => void saveCloudDevice());
  $("cloud-device-choice").addEventListener("change", () => {
    $("save-cloud-device").disabled = cloudDiscoveryBusy || !$("cloud-device-choice").value;
  });
  $("room_sensors").addEventListener("change", () => {
    renderPriorityPicker(Array.from($("priority_sensors").querySelectorAll("input:checked"), (input) => input.value));
    renderNearestThermostat($("nearest_thermostat").value);
    renderFireplacePicker(Array.from($("fireplace_rooms").querySelectorAll("input:checked"), (input) => input.value));
    filterRoomPicker();
    renderIndoorDiff();
    renderPreheatExample();
  });
  $("room-search").addEventListener("input", filterRoomPicker);
  $("show-room-sensors").addEventListener("change", filterRoomPicker);
  $("remove-room-sensors").addEventListener("click", () => {
    for (const input of $("room_sensors").querySelectorAll("input:checked")) {
      if (!input.value.startsWith("climate.")) input.checked = false;
    }
    $("room_sensors").dispatchEvent(new Event("change", { bubbles: true }));
  });
  $("priority_sensors").addEventListener("change", () => {
    limitPriorityPicker();
    renderIndoorDiff();
    renderPreheatExample();
  });
  $("offset_entity").addEventListener("change", () => { entityHints(); void refreshPumpModes(); });
  $("outdoor_entity").addEventListener("change", entityHints);
  $("price_entity").addEventListener("change", entityHints);
  $("price_provider").addEventListener("change", changePriceProvider);
  $("price_mode").addEventListener("change", renderPriceMode);
  $("refresh-history").addEventListener("click", () => void refreshHistory());
  $("refresh-energy").addEventListener("click", () => void refreshEnergy());
  $("refresh-savings").addEventListener("click", () => void refreshSavings());
  $("savings-days").addEventListener("change", () => void refreshSavings());
  $("refresh-sources").addEventListener("click", () => void refreshSources());
  $("refresh-auxiliary").addEventListener("click", () => void refreshAuxiliary());
  $("history-entity").addEventListener("change", () => { if (historyData) renderHistory(); });
  $("home-history-entity").addEventListener("change", () => {
    homeRoomSelection = $("home-history-entity").value;
    renderHomeHistory();
    renderRoomImpact();
  });
  $("refresh-plan").addEventListener("click", () => void refreshPlan());
  $("refresh-hot-water").addEventListener("click", () => void refreshHotWater());
  $("refresh-hot-water").addEventListener("click", () => void refreshHotWaterMonitor());
  $("hw-monitor-hours").addEventListener("change", () => void refreshHotWaterMonitor());
  $("refresh-learning").addEventListener("click", () => void refreshLearning());
  $("refresh-events").addEventListener("click", () => void refreshEvents());
  $("test-start").addEventListener("click", () => void testAction("start"));
  $("test-stop").addEventListener("click", () => void testAction("stop"));
  $("older-events").addEventListener("click", () => void refreshEvents(true));
  $("vacation-save").addEventListener("click", saveVacation);
  for (const id of ["vacation-start", "vacation-end"]) {
    whenPicker($(id));
    $(id).addEventListener("change", () => { vacationDraft = true; renderVacationDuration(); });
  }
  for (const chip of document.querySelectorAll("#vacation-presets [data-days]")) chip.addEventListener("click", () => vacationPreset(Number(chip.dataset.days)));
  $("vacation-cancel").addEventListener("click", () => void vacationAction({ action: "cancel" }));
  await refreshStatus();
  void showVersion();
  void refreshTest();
  void refreshDashboard();
  void refreshHistory();
  void refreshForecast();
  void refreshPlan();
  void refreshHotWater();
  void refreshHotWaterMonitor();
  void refreshCompressorDiagnostics();
  void refreshLearning();
  void refreshEnergy();
  void refreshSavings();
  void refreshAuxiliary(); void refreshSources();
  void refreshFireplaceSavings();
  window.setInterval(refreshFireplaceSavings, 3600000);
  window.setInterval(refreshStatus, 15000);
  window.setInterval(refreshLearning, 900000);
  window.setInterval(refreshHotWater, 120000);
  window.setInterval(refreshHotWaterMonitor, 60000);
  window.setInterval(refreshDashboard, 60000);
  window.setInterval(refreshHistory, 300000);
  window.setInterval(refreshCompressorDiagnostics, 300000);
  window.setInterval(refreshEnergy, 300000);
  window.setInterval(refreshSavings, 900000);
  window.setInterval(refreshAuxiliary, 120000);
  window.setInterval(refreshSources, 300000);
  window.setInterval(refreshForecast, 1800000);
  window.setInterval(refreshPlan, 300000);
  const resumeDashboard = () => {
    if (document.visibilityState === "hidden") return;
    // A suspended WebView can retain a stalled request after returning to the app.
    readRequests.get("status")?.abort();
    readRequests.get("settings")?.abort();
    void refreshStatus();
    void refreshDashboard();
    void refreshPlan();
    void refreshHotWater();
    void refreshHotWaterMonitor();
    void refreshTest();
    void refreshEnergy();
    void refreshAuxiliary(); void refreshSources();
  };
  document.addEventListener("visibilitychange", resumeDashboard);
  window.addEventListener("pageshow", resumeDashboard);
  window.addEventListener("online", resumeDashboard);
  window.addEventListener("focus", resumeDashboard);
  let lastWidth = window.innerWidth, resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (Math.abs(window.innerWidth - lastWidth) < 40) return;
      lastWidth = window.innerWidth;
      for (const id of ["energy-chart", "energy-cost-chart", "auxiliary-chart"]) chartRedraws.get($(id))?.();
      void refreshHistory();
      void refreshPlan();
      void refreshHotWater();
      void refreshLearning();
    }, 400);
  });
}

void init();
