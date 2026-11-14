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
const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "../src/app/(dashboard)/doctors/doctor-laser-weekdays.tsx"), "utf8");

const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.ReactJSX,
    target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  },
}).outputText;
const outputModule = { exports: {} };
const mockForm = ({ children, submitLabel }) =>
  React.createElement("form", { "data-save-label": submitLabel }, children);
new Function("require", "module", "exports", compiled)(
  (key) => {
    if (key === "react/jsx-runtime") return require("react/jsx-runtime");
    if (key === "@/components/action-feedback-form") return { ActionFeedbackForm: mockForm };
    if (key === "./actions") return { saveDoctorLaserWeekday: () => {} };
    throw new Error("Unexpected import: " + key);
  },
  outputModule,
  outputModule.exports,
);
const { DoctorLaserWeekdayPanel } = outputModule.exports;

const devices = [
  { device_key: "candela", name: "Candela", is_active: true },
  { device_key: "soprano", name: "Soprano", is_active: true },
  { device_key: "old", name: "Old", is_active: false },
];
const base = {
  doctor_id: "doc-1", doctor_name: "Synthetic Doctor",
  branch_id: "branch-1", branch_name: "Synthetic Branch",
  weekday: 0, excluded_device_keys: [],
};

function markup(rows, list = devices) {
  return renderToStaticMarkup(React.createElement(DoctorLaserWeekdayPanel, { rows, devices: list }));
}

test("new doctor is checked ON for every active device", () => {
  const html = markup([base]);
  assert.match(html, /الإثنين/);
  assert.match(html, /Candela/);
  assert.match(html, /Soprano/);
  assert.equal((html.match(/type="checkbox"/g) || []).length, 2);
  assert.equal((html.match(/checked=""/g) || []).length, 2);
  assert.doesNotMatch(html, /Old/);
  assert.match(html, /حفظ الأجهزة/);
  const noneActive = markup([base], devices.filter((device) => !device.is_active));
  assert.match(noneActive, /لا توجد أجهزة ليزر نشطة/);
  assert.doesNotMatch(noneActive, /data-save-label=/);
});

test("saved OFF exclusion unchecks one device only", () => {
  const html = markup([{ ...base, excluded_device_keys: ["candela"] }]);
  assert.equal((html.match(/checked=""/g) || []).length, 1);
  assert.match(html, /name="allowed_device_key"[^>]*value="soprano"/);
  assert.match(html, /value="candela"/);
});

test("all OFF remains unchecked without hiding non-laser workdays", () => {
  const html = markup([{ ...base, excluded_device_keys: ["candela", "soprano"] }]);
  assert.equal((html.match(/checked=""/g) || []).length, 0);
  assert.match(html, /الإثنين/);
});

test("new active clinic device defaults ON with unchanged saved exclusions", () => {
  const html = markup(
    [{ ...base, excluded_device_keys: ["candela"] }],
    [...devices, { device_key: "new", name: "New Device", is_active: true }],
  );
  assert.match(html, /New Device/);
  assert.equal((html.match(/checked=""/g) || []).length, 2);
});

test("multiple workday rows have separate doctor/branch/day forms", () => {
  const html = markup([base, { ...base, branch_id: "branch-2", branch_name: "Another Branch", weekday: 5 }]);
  assert.equal((html.match(/data-save-label=/g) || []).length, 2);
  assert.match(html, /Another Branch/);
  assert.match(html, /السبت/);
});

test("empty state is explicit", () => {
  assert.match(markup([]), /لا توجد أيام عمل مسجلة/);
});
