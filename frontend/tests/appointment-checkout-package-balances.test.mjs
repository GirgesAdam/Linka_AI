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
const sourceDir = join(here, "../src/app/(dashboard)/appointments/[appointmentId]");

function component(filename, mocks = {}) {
  const source = readFileSync(join(sourceDir, filename), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
  const exports = { exports: {} };
  const known = { "react/jsx-runtime": require("react/jsx-runtime"), ...mocks };
  new Function("require", "module", "exports", compiled)(
    (key) => {
      if (Object.prototype.hasOwnProperty.call(known, key)) return known[key];
      throw new Error("Unexpected UI test import: " + key);
    },
    exports,
    exports.exports,
  );
  return exports.exports;
}

const defaultPackage = {
  id: "full", name: "Full Body Synthetic", service_id: "full-service",
  laser_device_key: "candela", laser_device_name: "Candela",
  status: "active", effective_status: "active",
  sessions_purchased: 3, opening_sessions_remaining: null,
  sessions_total_known: true, sessions_reserved: 0,
  sessions_consumed: 2, sessions_remaining: 1,
};

function packageMarkup(packages, loadFailed = false) {
  const { PatientPackageBalances } = component("patient-package-balances.tsx");
  return renderToStaticMarkup(
    React.createElement(PatientPackageBalances, { packages, loadFailed }),
  );
}

function checkoutMarkup({ balanceMinor = 0, completesVisit = true, alreadyCompleted = false } = {}) {
  const form = ({ children, successHref, successMessage }) => React.createElement(
    "form",
    { "data-success-href": successHref, "data-success-message": successMessage },
    children,
  );
  const button = ({ children, disabled }) => React.createElement("button", { disabled }, children);
  const icon = () => React.createElement("span");
  const { AppointmentPaymentForm } = component("appointment-payment-form.tsx", {
    react: React,
    "lucide-react": { CircleDollarSign: icon, Zap: icon },
    "@/components/idempotent-mutation-form": {
      IdempotentMutationForm: form,
      PendingSubmitButton: button,
    },
    "@/lib/format": { formatMoney: (n) => String(n / 100) },
    "./actions": { recordAppointmentPayment: () => undefined },
  });
  return renderToStaticMarkup(React.createElement(AppointmentPaymentForm, {
    appointmentId: "synthetic-id",
    patientId: "synthetic-patient",
    currency: "EGP",
    subtotalMinor: balanceMinor,
    servicePriceMinor: balanceMinor,
    discountMinor: 0,
    netPaidMinor: 0,
    balanceMinor,
    pulseCheckout: null,
    completesVisit,
    alreadyCompleted,
  }));
}

test("two package balances and pulse-independent list: show every package, including zero", () => {
  const html = packageMarkup([
    { ...defaultPackage, id: "face", name: "Face Synthetic", sessions_consumed: 1, sessions_remaining: 2 },
    { ...defaultPackage, id: "full", name: "Full Body Synthetic" },
    { ...defaultPackage, id: "expired", name: "Expired Synthetic", effective_status: "expired", sessions_remaining: 0 },
    { ...defaultPackage, id: "cancelled", name: "Cancelled Synthetic", effective_status: "cancelled", sessions_remaining: 0 },
  ]);
  for (const name of ["Face Synthetic", "Full Body Synthetic", "Expired Synthetic", "Cancelled Synthetic"]) {
    assert.match(html, new RegExp(name));
  }
  assert.match(html, /data-package-status="expired"/);
  assert.match(html, /data-package-status="cancelled"/);
  assert.ok(html.indexOf('data-package-status="active"') < html.indexOf('data-package-status="expired"'));
});

test("used/reserved/remaining: use backend remaining rather than subtracting again", () => {
  const html = packageMarkup([{
    ...defaultPackage, id: "reserved", sessions_purchased: 5, sessions_consumed: 1,
    sessions_reserved: 2, sessions_remaining: 2,
  }]);
  assert.match(html, /إجمالي الجلسات: ٥/);
  assert.match(html, /تم استخدامها: ١/);
  assert.match(html, /محجوزة: ٢/);
  assert.match(html, /المتبقي: ٢/);
  assert.doesNotMatch(html, /المتبقي: ٠/);
});

test("known imported opening balance shows historical consumption, not fictional remaining", () => {
  const html = packageMarkup([{
    ...defaultPackage, sessions_purchased: 10, opening_sessions_remaining: 4,
    sessions_total_known: true, sessions_consumed: 1, sessions_reserved: 1,
    sessions_remaining: 2,
  }]);
  assert.match(html, /إجمالي الجلسات: ١٠/);
  assert.match(html, /تم استخدامها: ٧/);
  assert.match(html, /محجوزة: ١/);
  assert.match(html, /المتبقي: ٢/);
});

test("unknown imported total remains unknown and uses authoritative remaining", () => {
  const html = packageMarkup([{
    ...defaultPackage, sessions_purchased: 0, opening_sessions_remaining: 5,
    sessions_total_known: false, sessions_consumed: 1, sessions_reserved: 1,
    sessions_remaining: 3,
  }]);
  assert.match(html, /إجمالي الجلسات: غير محدد/);
  assert.match(html, /مستخدمة منذ الاستيراد: ١/);
  assert.match(html, /المتبقي: ٣/);
});

test("API load failure cannot masquerade as no customer packages", () => {
  const failed = packageMarkup([], true);
  assert.match(failed, /role="alert"/);
  assert.match(failed, /تعذر تحميل أرصدة/);
  assert.doesNotMatch(failed, /لا توجد باكيدجات مسجلة/);
  const empty = packageMarkup([]);
  assert.match(empty, /لا توجد باكيدجات مسجلة/);
  assert.doesNotMatch(empty, /role="alert"/);
});

test("zero-balance checkout has completion button and no payment-method prompt", () => {
  const html = checkoutMarkup({ balanceMinor: 0, completesVisit: true });
  assert.match(html, /تسجيل الحساب وإنهاء الموعد/);
  assert.match(html, /visit_saved=checkout_complete/);
  assert.match(html, /name="payment_method" value="cash"/);
  assert.doesNotMatch(html, /<select name="payment_method"/);
  assert.match(html, /تم تسجيل الحساب وإنهاء الموعد بنجاح/);
});

test("future prepayment does not claim completion", () => {
  const html = checkoutMarkup({ balanceMinor: 50000, completesVisit: false, alreadyCompleted: false });
  assert.match(html, /تسجيل دفعة مقدمة دون إتمام الموعد/);
  assert.match(html, /سيظل الموعد مفتوحاً/);
  assert.doesNotMatch(html, /visit_saved=checkout_complete/);
  assert.match(html, /<select name="payment_method"/);
});

test("already completed visit can collect outstanding balance without completing again", () => {
  const html = checkoutMarkup({ balanceMinor: 20000, completesVisit: false, alreadyCompleted: true });
  assert.match(html, /تسجيل دفعة إضافية/);
  assert.match(html, /تم تسجيل الدفعة الإضافية بنجاح/);
  assert.doesNotMatch(html, /visit_saved=checkout_complete/);
});

/*
 * Render the actual async appointment page, not just PatientPackageBalances.
 * All request payloads are synthetic and no HTTP connection is made.
 */
async function appointmentPageMarkup({ packages = [], pulses = [], packageError = false } = {}) {
  const ui = ({ children }) => React.createElement("div", null, children);
  const icon = () => React.createElement("span");
  const link = ({ children, href }) => React.createElement("a", { href }, children);
  const { PatientPackageBalances } = component("patient-package-balances.tsx");

  const appointment = {
    id: "synthetic-appt", patient_id: "synthetic-patient", service_id: "service-1",
    branch_id: "branch-1", doctor_id: "doctor-1", start_at: "2026-11-01T09:00:00Z",
    duration_minutes: 30, status: "completed", currency: "EGP", price_minor: 0,
    billing_context: "standard", patient_package_id: null, package_external_id: null,
    laser_device_key: null, laser_device_name: null, laser_pulses_used: null,
  };
  const detail = {
    appointment, allowed_actions: [],
    patient: { id: "synthetic-patient", name: "Synthetic Patient", phone: "" },
    service: { name: "Synthetic Service" },
    doctor: { name: "Synthetic Doctor" },
    automations: [], history: [],
    cancellation_override_required: false, can_override_cancellation_policy: false,
  };
  const payments = {
    appointment_id: appointment.id, patient_id: appointment.patient_id, currency: "EGP",
    subtotal_minor: 0, price_minor: 0, discount_minor: 0, service_price_minor: 0,
    products_total_minor: 0, additional_services_total_minor: 0,
    package_sales_total_minor: 0, pulse_pack_sales_total_minor: 0,
    pulse_overage_total_minor: 0, gross_paid_minor: 0, refunded_minor: 0,
    net_paid_minor: 0, balance_minor: 0, billing_context: "standard",
    transactions: [], payment_status: "paid", can_refund: false,
  };

  const requests = async (url) => {
    if (url === "/booking/appointments/synthetic-appt/operations") return detail;
    if (url === "/payments/appointments/synthetic-appt") return payments;
    if (url === "/booking/patients/synthetic-patient/packages") {
      if (packageError) throw Error("synthetic API failure");
      return packages;
    }
    if (url === "/booking/patients/synthetic-patient/pulse-balance") return pulses;
    if (url.endsWith("/pulse-settlement")) return null;
    return [];
  };

  const actionExports = Object.fromEntries([
    "addAppointmentProduct", "cancelAppointment", "purchasePackageForAdditionalService",
    "purchasePackageFromAppointment", "refundAppointmentPayment",
    "removeAppointmentAdditionalService", "removeAppointmentProduct",
    "updateAppointmentStatus", "updateLaserPulses",
  ].map((name) => [name, async () => {}]));
  const icons = Object.fromEntries([
    "ArrowRight", "CalendarClock", "CircleDollarSign", "CircleX", "History",
    "PackagePlus", "ReceiptText", "Stethoscope", "Trash2", "UserRound",
    "UserX", "Workflow",
  ].map((name) => [name, icon]));
  const { default: Page } = component("page.tsx", {
    "lucide-react": icons,
    "@/components/permission-link": { PermissionLink: link },
    "@/components/page-header": { PageHeader: ui },
    "@/components/idempotent-mutation-form": {
      IdempotentMutationForm: ui, PendingSubmitButton: ui,
    },
    "@/components/ui/badge": { Badge: ui },
    "@/components/ui/button": { Button: ui, buttonVariants: () => "" },
    "@/components/ui/card": {
      Card: ui, CardContent: ui, CardHeader: ui, CardTitle: ui,
    },
    "@/components/ui/input": { Input: () => React.createElement("input") },
    "@/components/ui/status-badge": { StatusBadge: ui },
    "@/lib/format": {
      formatDateTime: (value) => String(value),
      formatMoney: (n) => String(n / 100) + " EGP",
    },
    "@/lib/status": {
      appointmentLabels: {}, labelForStatus: (value) => String(value),
      toneForStatus: () => "neutral",
    },
    "@/lib/tia/api": { tiaRequest: requests },
    "./actions": actionExports,
    "./additional-service-form": { AdditionalServiceForm: ui },
    "./appointment-payment-form": { AppointmentPaymentForm: ui },
    "./patient-package-balances": { PatientPackageBalances },
    "./service-editor": { AppointmentServiceEditor: ui },
  });
  return renderToStaticMarkup(await Page({
    params: Promise.resolve({ appointmentId: appointment.id }),
    searchParams: Promise.resolve({}),
  }));
}

test("actual appointment page: no packages and no pulses retains empty-state section", async () => {
  const html = await appointmentPageMarkup();
  assert.match(html, /رصيد العميل وقت الحساب/);
  assert.match(html, /لا توجد باكيدجات مسجلة لهذا العميل/);
  assert.doesNotMatch(html, /Pulse متبقية/);
});

test("actual appointment page: packages only are shown without pulse balance", async () => {
  const html = await appointmentPageMarkup({ packages: [defaultPackage] });
  assert.match(html, /رصيد العميل وقت الحساب/);
  assert.match(html, /Full Body Synthetic/);
  assert.match(html, /المتبقي: ١/);
  assert.doesNotMatch(html, /Pulse متبقية/);
});

test("actual appointment page: pulse-only balance remains visible with package empty state", async () => {
  const html = await appointmentPageMarkup({
    pulses: [{
      device_key: "candela", device_name: "Candela Synthetic",
      pulses_purchased: 1500, pulses_consumed: 0,
      pulses_remaining: 1500, active_pack_count: 1,
    }],
  });
  assert.match(html, /رصيد العميل وقت الحساب/);
  assert.match(html, /Candela Synthetic/);
  assert.match(html, /١٬٥٠٠ Pulse متبقية/);
  assert.match(html, /لا توجد باكيدجات مسجلة لهذا العميل/);
});

test("actual appointment page: packages API error shows error without pretending list is empty", async () => {
  const html = await appointmentPageMarkup({ packageError: true });
  assert.match(html, /رصيد العميل وقت الحساب/);
  assert.match(html, /تعذر تحميل أرصدة باكيدجات العميل/);
  assert.match(html, /role="alert"/);
  assert.doesNotMatch(html, /لا توجد باكيدجات مسجلة لهذا العميل/);
});
