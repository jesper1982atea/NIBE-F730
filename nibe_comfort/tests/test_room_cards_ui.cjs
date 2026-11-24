const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("nibe_comfort/static/app.js", "utf8").replace("void init();", "");
const nodes = new Map();
function element(tagName = "div") {
  return {
    tagName, textContent: "", children: [], hidden: false,
    classList: { toggle() {} },
    setAttribute() {}, addEventListener() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    get childNodes() { return this.children; },
    get innerHTML() { return JSON.stringify(this.children); },
  };
}
const context = vm.createContext({
  document: {
    createElement: element,
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
  },
});
vm.runInContext(source, context);
vm.runInContext(`
  renderHomeHistory = () => {};
  renderComfort = () => {};
  renderRadiatorStatus = () => {};
  settings = { priority_sensors: ["climate.priority"], fireplace_rooms: [], target_temperature: 21 };
`, context);

function render(state) {
  context.testState = state;
  vm.runInContext("lastStatus = testState; renderRoomImpact(); renderStatusExtras(testState)", context);
  return nodes.get("room-impact-list").children[0].children[1].children;
}

const rooms = [
  { entity_id: "climate.priority", name: "Element", temperature: 22, setpoint: 21, priority: true },
  { entity_id: "sensor.room", name: "Rumsgivare", temperature: 20 },
];
const repeated = "Element är en termostat som mäter nära elementet och kan visa för varmt; den styr värmen.";
let cards = render({ rooms, indoor_rooms: ["climate.priority"], house_indoor: 21, warnings: [repeated, "Elpris saknas"], room_sensor_warnings: [repeated] });
assert.match(cards[0].children.at(-1).children[0].textContent, /Ingår i styrande snitt/);
assert.match(cards[0].children.at(-1).children[0].textContent, /ingår i husets snitt/);
assert.match(cards[0].children.at(-1).children.at(-1).textContent, /kan påverkas/);
assert.match(cards[1].children.at(-1).children[0].textContent, /Ingår inte i styrande snitt/);
assert.doesNotMatch(cards[1].children.at(-1).children.at(-1).textContent, /element/);
assert.deepEqual(nodes.get("status-warnings").children.map((item) => item.textContent), ["Elpris saknas"]);

cards = render({ rooms, indoor_rooms: ["sensor.room"], indoor_fallback: true, warnings: [], room_sensor_warnings: [] });
assert.match(cards[0].children.at(-1).children[0].textContent, /Ingår inte/);
assert.match(cards[1].children.at(-1).children[0].textContent, /Ingår i styrande snitt/);
assert.equal(nodes.get("status-warnings").hidden, true);

cards = render({ rooms, indoor_rooms: null, error_message: "Pausad", warnings: [], room_sensor_warnings: [] });
assert.match(cards[0].children.at(-1).children[0].textContent, /kan inte bedömas/);
assert.doesNotMatch(cards[0].children.at(-1).children[0].textContent, /husets snitt/);
console.log("UI room cards: actual control roles, sensor notes and genuine warnings passed.");
