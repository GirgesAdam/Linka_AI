import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require("typescript");
const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../src/app/(dashboard)/doctors/doctor-management.tsx"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
const mod = { exports: {} };
new Function("require", "module", "exports", compiled)((key) => {
  if (key === "react" || key === "react/jsx-runtime") return require(key);
  if (key === "next/navigation" || key === "./actions") return {};
  if (key === "lucide-react") return new Proxy({}, { get: () => () => null });
  if (key === "@/components/ui/button") return { Button: ({ children }) => React.createElement("button", {}, children) };
  if (key === "@/components/ui/card") return {};
  throw new Error("Unexpected import " + key);
}, mod, mod.exports);
const { ScheduleFields } = mod.exports;
const devices = [
  { device_key: "candela", name: "Candela", is_active: true },
  { device_key: "soprano", name: "Soprano", is_active: true },
  { device_key: "old", name: "Old", is_active: false },
];
const interval = { weekday: 0, start_time: "09:00", end_time: "17:00" };
const base = { doctor_id: "doc-1", branch_id: "branch-1", weekday: 0, excluded_device_keys: [] };
const markup = (rows = [base], list = devices, hours = [interval]) =>
  renderToStaticMarkup(React.createElement(ScheduleFields, { initialHours: hours, devices: list, rows }));
const count = (html, pattern) => (html.match(pattern) || []).length;

test("new workday has all active devices ON next to its time inputs", () => {
  const html = markup([]);
  assert.match(html, /الإثنين/);
  assert.match(html, /type="time"[^>]*value="09:00"/);
  assert.match(html, /Candela/);
  assert.match(html, /Soprano/);
  assert.equal(count(html, /type="checkbox"/g), 2);
  assert.equal(count(html, /checked=""/g), 2);
  assert.doesNotMatch(html, /Old/);
});
test("saved OFF exclusion unchecks one device only", () => {
  const html = markup([{ ...base, excluded_device_keys: ["candela"] }]);
  assert.equal(count(html, /checked=""/g), 1);
  assert.match(html, /laser_weekday_exclusions_json/);
});
test("all OFF remains unchecked and preserves working hours", () => {
  const html = markup([{ ...base, excluded_device_keys: ["candela", "soprano"] }]);
  assert.equal(count(html, /checked=""/g), 0);
  assert.equal(count(html, /type="time"/g), 2);
});
test("new active device defaults ON", () => {
  const html = markup([{ ...base, excluded_device_keys: ["candela"] }], [...devices, { device_key: "new", name: "New Device", is_active: true }]);
  assert.match(html, /New Device/);
  assert.equal(count(html, /checked=""/g), 2);
});
test("two shifts in one day share one set of checkboxes; separate days do not", () => {
  const html = markup([base], devices, [interval, { ...interval, start_time: "18:00", end_time: "20:00" }, { ...interval, weekday: 5 }]);
  assert.equal(count(html, /type="time"/g), 6);
  assert.equal(count(html, /type="checkbox"/g), 4);
});
test("missing device data does not submit empty exclusions", () => {
  assert.doesNotMatch(markup([], null), /laser_weekday_exclusions_json/);
  assert.equal(count(markup([], devices, []), /type="checkbox"/g), 0);
});
