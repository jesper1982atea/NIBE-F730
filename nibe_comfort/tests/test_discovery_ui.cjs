const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("nibe_comfort/static/app.js", "utf8").replace("void init();", "");
const defaults = {
  enabled: false, offset_entity: "", outdoor_entity: "", base_offset: 0,
  min_offset: -3, max_offset: 3, room_sensors: ["sensor.user_selected"],
};
const offsetId = "number.pump_heating_offset_climate_system_1";
const outdoorId = "sensor.pump_current_outd_temp_bt1";
const result = { offset_entity: offsetId, outdoor_entity: outdoorId, message: "NIBE hittad" };

async function run(config, response = result, state = "3", fail = false) {
  const context = vm.createContext({
    document: {},
    AbortSignal,
    fetch: async () => {
      if (fail) throw new Error("Home Assistant frånkopplad");
      return { ok: true, json: async () => response };
    },
    config: structuredClone(config),
    mockEntities: [{ entity_id: offsetId, state }],
  });
  vm.runInContext(source, context);
  await vm.runInContext("settings = config; entities = mockEntities; findPump()", context);
  return JSON.parse(vm.runInContext("JSON.stringify({settings, discoveryError})", context));
}

(async () => {
  let actual = await run(defaults);
  assert.equal(actual.settings.offset_entity, offsetId);
  assert.equal(actual.settings.outdoor_entity, outdoorId);
  assert.equal(actual.settings.base_offset, 3);
  assert.equal(actual.settings.enabled, false);
  assert.deepEqual(actual.settings.room_sensors, defaults.room_sensors);
  actual = await run({ ...defaults, offset_entity: "number.manual", outdoor_entity: "sensor.manual", base_offset: 1 });
  assert.equal(actual.settings.offset_entity, "number.manual");
  assert.equal(actual.settings.outdoor_entity, "sensor.manual");
  assert.equal(actual.settings.base_offset, 1);
  actual = await run({ ...defaults, offset_entity: offsetId });
  assert.equal(actual.settings.outdoor_entity, outdoorId);
  actual = await run({ ...defaults, offset_entity: "number.other_pump" });
  assert.equal(actual.settings.outdoor_entity, "");
  for (const value of ["unavailable", "unknown", "10"]) {
    actual = await run(defaults, result, value);
    assert.equal(actual.settings.base_offset, 0);
  }
  actual = await run(defaults, { offset_entity: "", outdoor_entity: "", message: "Flera pumpar" });
  assert.equal(actual.settings.offset_entity, "");
  actual = await run(defaults, result, "3", true);
  assert.match(actual.discoveryError, /frånkopplad/);
  assert.equal(actual.settings.offset_entity, "");
  actual = await run({ ...defaults, price_entity: "" }, { ...result, price_entity: "sensor.tibber" });
  assert.equal(actual.settings.price_entity, "sensor.tibber");
  assert.equal(actual.settings.enabled, false);
  actual = await run({ ...defaults, price_entity: "sensor.manual_price" }, { ...result, price_entity: "sensor.tibber" });
  assert.equal(actual.settings.price_entity, "sensor.manual_price");
  console.log("UI discovery: automatic pairing, manual choices, room selection, bounds and errors passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
