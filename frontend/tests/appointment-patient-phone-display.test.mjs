import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require("typescript");

const root = join(dirname(fileURLToPath(import.meta.url)), "../src");
const path = "app/(dashboard)/appointments";
const componentSource = readFileSync(join(root, path, "patient-appointment-identity.tsx"), "utf8");
const pageSource = readFileSync(join(root, path, "page.tsx"), "utf8");
const types = readFileSync(join(root, "lib/types.ts"), "utf8");

const compiled = ts.transpileModule(componentSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.ReactJSX,
    target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  },
}).outputText;
const outputModule = { exports: {} };
new Function("require", "module", "exports", compiled)(
  (key) => {
    if (key === "react/jsx-runtime") return require("react/jsx-runtime");
    throw new Error("Unexpected import: " + key);
  },
  outputModule,
  outputModule.exports,
);
const { PatientAppointmentIdentity } = outputModule.exports;
function render(patient) {
  return renderToStaticMarkup(React.createElement(
    "a", { href: "/appointments/synthetic-id", dir: "rtl" },
    React.createElement(PatientAppointmentIdentity, { patient }),
  ));
}
const cardUsage = "<PatientAppointmentIdentity patient={patientDetails.get(appointment.patient_id)} />";

test("Egyptian phone is shown in full next to Arabic name and is LTR", () => {
  const html = render({ name: "عميل تجريبي", phone: "01012345678" });
  assert.match(html, /عميل تجريبي/);
  assert.match(html, /<bdi dir="ltr"[^>]*>01012345678<\/bdi>/);
  assert.ok(html.indexOf("عميل تجريبي") < html.indexOf("01012345678"));
  assert.equal((html.match(/<a /g) || []).length, 1);
});

test("international numbers retain their full notation and direction", () => {
  const value = "+44 7700 900123";
  const html = render({ name: "Test Patient", phone: value });
  assert.ok(html.includes(value));
  assert.match(html, /<bdi dir="ltr"/);
});

test("missing phone, empty phone, or unknown patient show no placeholder", () => {
  for (const patient of [
    { name: "عميل بدون رقم", phone: null },
    { name: "عميل بدون رقم", phone: "" },
    { name: "عميل بدون رقم", phone: "  " },
    undefined,
  ]) {
    const html = render(patient);
    assert.doesNotMatch(html, /<bdi/);
    assert.doesNotMatch(html, /null|undefined/i);
    assert.match(html, /عميل بدون رقم|عميل/);
  }
});

test("phone is never truncated; responsive name and phone wrap independently", () => {
  const html = render({ name: "عميل باسم طويل جداً للاختبار", phone: "+201012345678901234" });
  assert.match(html, /flex-wrap/);
  assert.match(html, /break-words/);
  assert.match(html, /whitespace-nowrap/);
  assert.doesNotMatch(html, /truncate|text-ellipsis/);
  assert.ok(html.includes("+201012345678901234"));
});

test("desktop normal and blocked-period cards use the same identity component", () => {
  const desktop = pageSource.split("function DailySchedule(", 2)[1].split("export default async function AppointmentsPage(", 1)[0];
  assert.match(desktop, /isBlocked \? \(/);
  assert.match(desktop, /period\.appointments\.map/);
  assert.equal(desktop.split(cardUsage).length - 1, 3);
  assert.match(desktop, /مواعيد خارج ساعات العمل الحالية/);
});

test("mobile agenda uses the same identity component", () => {
  const mobile = pageSource.split("function MobileAgenda(", 2)[1].split("function DailySchedule(", 1)[0];
  assert.match(mobile, /aria-label="mobile appointment agenda"/);
  assert.equal(mobile.split(cardUsage).length - 1, 1);
});

test("one batch fetch requests phone for appointment cards, with existing lookup type", () => {
  assert.match(pageSource, /lookupPatientsByIds\(appointmentPatientIds, true\)/);
  assert.match(pageSource, /patient_ids: \[\.\.\.new Set\(patientIds\)\]/);
  assert.match(pageSource, /include_phone: includePhone/);
  assert.match(types, /export interface AppointmentPatientLookup\s*{/);
  assert.match(types, /phone: string \| null/);
  assert.match(pageSource, /phone: patient\.phone/);
});

test("selected patient is also mapped with phone, while heading uses only name", () => {
  assert.match(pageSource, /patientDetails\.set\(selectedPatient\.id,\s*{[\s\S]*?phone: selectedPatient\.phone/);
  assert.match(pageSource, /patientDetails\.get\(selectedPatient\.id\)\?\.name/);
  assert.equal(pageSource.split(cardUsage).length - 1, 4);
});
