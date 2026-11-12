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
const source = readFileSync(
  join(here, "../src/app/(dashboard)/appointments/[appointmentId]/additional-service-form.tsx"),
  "utf8",
);

function htmlFor({
  serviceId = "face",
  deviceKey = "candela",
  billingMethod = "package",
  patientPackageId = "face-package",
  packages,
  failed = false,
  service,
} = {}) {
  let index = 0;
  const selected = [
    serviceId, deviceKey, billingMethod, "", "purchase_pack", "", patientPackageId,
  ];
  const mockReact = {
    ...React,
    useState: (initialValue) => React.useState(selected[index++] ?? initialValue),
    useMemo: React.useMemo,
  };
  const fauxForm = ({ children, successMessage, successHref }) =>
    React.createElement(
      "div",
      { "data-success": successMessage, "data-success-href": successHref },
      children,
    );
  const fauxButton = ({ disabled, children }) =>
    React.createElement("button", { disabled }, children);
  const icon = () => React.createElement("span");
  const mocks = {
    react: mockReact,
    "react/jsx-runtime": require("react/jsx-runtime"),
    "lucide-react": { Plus: icon, Zap: icon },
    "@/components/idempotent-mutation-form": {
      IdempotentMutationForm: fauxForm,
      PendingSubmitButton: fauxButton,
    },
    "@/lib/format": { formatMoney: (minor) => (minor / 100).toFixed(2) + " EGP" },
    "./actions": { addAppointmentAdditionalService: () => undefined },
  };
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
  const compiledModule = { exports: {} };
  const scopedRequire = (name) => {
    if (Object.prototype.hasOwnProperty.call(mocks, name)) return mocks[name];
    throw new Error("Unexpected module in isolated UI test: " + name);
  };
  new Function("require", "module", "exports", output)(
    scopedRequire, compiledModule, compiledModule.exports,
  );
  const defaults = [
    { id: "primary", name: "Primary", is_active: true, requires_laser_device: true, price_minor: 50000, currency: "EGP" },
    { id: "face", name: "Face", is_active: true, requires_laser_device: true, price_minor: 20000, currency: "EGP" },
  ];
  const props = {
    appointmentId: "synthetic-appointment",
    patientId: "synthetic-patient",
    primaryServiceId: "primary",
    appointmentStartAt: "2026-11-10T12:00:00+00:00",
    services: service ? [defaults[0], service] : defaults,
    devicePrices: [{ service_id: "face", device_key: "candela", device_name: "Candela", price_minor: 20000, configured: true, currency: "EGP" }],
    patientPackages: packages ?? [{
      id: "face-package", service_id: "face", patient_id: "synthetic-patient",
      name: "Synthetic Face Package", laser_device_key: "candela", laser_device_name: "Candela",
      status: "active", effective_status: "active", sessions_remaining: 2,
      purchased_at: "2026-11-01T00:00:00Z", expires_at: null,
    }],
    packageLoadFailed: failed,
    pulseBalances: [],
    pulsePackOffers: [],
    pulseSettings: [],
  };
  return renderToStaticMarkup(React.createElement(compiledModule.exports.AdditionalServiceForm, props));
}

test("existing-package option shows matching package, session count and zero extra charge", () => {
  const html = htmlFor();
  assert.match(html, /استخدام باكيدج موجودة/);
  assert.match(html, /Synthetic Face Package/);
  assert.match(html, /٢ جلسة/);
  assert.match(html, /المتبقي بعدها 1 جلسة/);
  assert.match(html, /0 EGP/);
  assert.match(html, /data-success="تمت إضافة جلسة Face من باكيدج Synthetic Face Package/);
  assert.match(html, /visit_saved=existing_package_service/);
  assert.doesNotMatch(html, /<button disabled=""/);
});

test("unrelated, wrong-device, expired, cancelled, exhausted and future packages cannot be chosen", () => {
  const valid = {
    id: "valid", service_id: "face", patient_id: "synthetic-patient",
    name: "Valid synthetic", laser_device_key: "candela", laser_device_name: "Candela",
    status: "active", effective_status: "active", sessions_remaining: 2,
    purchased_at: "2026-11-01T00:00:00Z", expires_at: null,
  };
  const invalid = [
    { ...valid, id: "bad-service", name: "Wrong Service", service_id: "primary" },
    { ...valid, id: "bad-device", name: "Wrong Device", laser_device_key: "different" },
    { ...valid, id: "expired", name: "Expired Package", expires_at: "2026-11-09" },
    { ...valid, id: "cancelled", name: "Cancelled Package", status: "cancelled" },
    { ...valid, id: "exhausted", name: "Exhausted Package", sessions_remaining: 0 },
    { ...valid, id: "future", name: "Future Package", purchased_at: "2026-11-11T00:00:00Z" },
  ];
  const html = htmlFor({ packages: [valid, ...invalid], patientPackageId: "valid" });
  assert.match(html, /Valid synthetic/);
  for (const pkg of invalid) assert.doesNotMatch(html, new RegExp(pkg.name));
});

test("no eligible packages is not conflated with API load failure", () => {
  const unavailable = htmlFor({ packages: [], patientPackageId: "" });
  assert.match(unavailable, /لا توجد باكيدج صالحة/);
  assert.match(unavailable, /<button disabled=""/);
  const failed = htmlFor({ packages: [], failed: true, patientPackageId: "" });
  assert.match(failed, /تعذر تحميل باكيدجات العميل/);
  assert.match(failed, /إعادة المحاولة/);
  assert.doesNotMatch(failed, /لا توجد باكيدج صالحة/);
});

test("non-laser pricing does not claim base service price is the doctor's final price", () => {
  const html = htmlFor({
    serviceId: "consult",
    deviceKey: "",
    billingMethod: "standard",
    patientPackageId: "",
    service: { id: "consult", name: "Consult", is_active: true, requires_laser_device: false, price_minor: 15000, currency: "EGP" },
  });
  assert.match(html, /السعر النهائي/);
  assert.match(html, /حسب تسعير الطبيب/);
  assert.doesNotMatch(html, /150.00 EGP/);
  assert.doesNotMatch(html, /<button disabled=""/);
});

test("pulse billing choice remains visible for laser service", () => {
  const html = htmlFor({ billingMethod: "pulse", patientPackageId: "" });
  assert.match(html, /الحساب بالـPulses/);
  assert.match(html, /عدد الـPulses المستخدمة فعليًا/);
  assert.doesNotMatch(html, /تمت إضافة جلسة Face من باكيدج/);
});
