const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("nibe_comfort/static/app.js", "utf8").replace("void init();", "");
const nodes = new Map();
function element() {
  return {
    textContent: "", children: [], value: "24",
    getClientRects: () => [{}],
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); },
  };
}
const report = {
  current: "charging", price_control_enabled: false, sample_count: 12, last_sample: 1200,
  current_reported_at: null, truncated: false, message: "Observerade prover.",
  charges: [{ start: 1000, end: 1100, observed_seconds: 60, start_known: true,
    start_values: { bt6: 44, mode: "Normal" }, end_values: { bt6: 46 }, alarm_result: "unknown" }],
  offset_changes: [{ timestamp: 1100, previous: 3, value: 4, alarm_result: "observed" }],
};
const context = vm.createContext({
  document: {
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
    createElement: element,
  },
  window: { setTimeout, clearTimeout },
  AbortController,
  fetch: async () => ({ ok: true, json: async () => report }),
});
vm.runInContext(source, context);

(async () => {
  await vm.runInContext("refreshHotWaterMonitor()", context);
  assert.match(nodes.get("hw-monitor-control").textContent, /AV/);
  assert.match(nodes.get("hw-monitor-status").textContent, /varmvattenladdning/);
  const charge = nodes.get("hw-monitor-charges").children[0];
  assert.match(charge.children[2].textContent, /Larmresultat okänt/);
  assert.match(charge.children[3].textContent, /Tank BT6/);
  const offset = nodes.get("hw-monitor-offsets").children[0];
  assert.match(offset.children[2].textContent, /Larm observerat/);
  console.log("UI hot water monitor: control state, readings and alarm uncertainty passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
