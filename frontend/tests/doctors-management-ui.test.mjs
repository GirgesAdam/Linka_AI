import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const root = join(dirname(fileURLToPath(import.meta.url)), "../src/app/(dashboard)");
const doctorsPage = readFileSync(join(root, "doctors/page.tsx"), "utf8");
const doctorsUI = readFileSync(join(root, "doctors/doctor-management.tsx"), "utf8");
const servicePage = readFileSync(join(root, "services/page.tsx"), "utf8");
const serviceActions = readFileSync(join(root, "services/actions.ts"), "utf8");
const doctorActionsSource = readFileSync(join(root, "doctors/actions.ts"), "utf8");

let api;
const revalidated = [];
class FakeTiaApiError extends Error {
  constructor(status, message, technicalMessage) {
    super(message);
    this.status = status;
    this.technicalMessage = technicalMessage;
  }
}
const compiled = ts.transpileModule(doctorActionsSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  },
}).outputText;
const mod = { exports: {} };
new Function("require", "module", "exports", compiled)(
  (name) => {
    if (name === "next/cache") return { revalidatePath: (url) => revalidated.push(url) };
    if (name === "@/lib/tia/api") return {
      tiaRequest: (...args) => api(...args),
      TiaApiError: FakeTiaApiError,
    };
    throw new Error("Unexpected import " + name);
  },
  mod,
  mod.exports,
);
const actions = mod.exports;
function form(entries) {
  const data = new FormData();
  for (const [name, value] of entries) data.append(name, value);
  return data;
}
const initial = { notice: null, error: null, saved: null };

test("doctors page loads separate laser rows and device list only with clinic operations access", () => {
  assert.match(doctorsPage, /canManageClinic = canAccessSection\(ctx\.workspace, "clinic_operations"\)/);
  assert.match(doctorsPage, /canManageClinic \? tiaRequest<DoctorLaserWeekday\[\]>\("\/clinic\/doctor-laser-weekdays"\)/);
  assert.match(doctorsPage, /canManageClinic \? tiaRequest<ClinicLaserDevice\[\]>\("\/inventory\/laser-devices"\)/);
  assert.match(doctorsPage, /\{canManageClinic && \(adminDoctors === null \? \(/);
  assert.match(doctorsPage, /<DoctorManagementPanel doctors=\{adminDoctors\}/);
});

test("services page no longer contains a duplicate doctor laser editor or endpoint", () => {
  assert.doesNotMatch(servicePage, /DoctorLaserWeekdayPanel|doctorLaserDays|\/clinic\/doctor-laser-weekdays/);
  assert.doesNotMatch(serviceActions, /saveDoctorLaserWeekday|\/clinic\/doctor-laser-weekdays/);
});

test("doctor editor includes weekly hours beside doctor/branch/day laser device controls", () => {
  assert.match(doctorsUI, /<DoctorProfileForm doctor=\{doctor\} \/>/);
  assert.match(doctorsUI, /<DoctorScheduleForm doctor=\{doctor\} \/>/);
  assert.match(doctorsUI, /أجهزة الليزر حسب يوم العمل والفرع/);
  assert.match(doctorsUI, /<DoctorLaserWeekdayPanel/);
  assert.match(doctorsUI, /row\.doctor_id === doctor\.id/);
  assert.match(doctorsUI, /doctor\.service_categories\.includes\("laser"\)/);
});

test("laser API errors never masquerade as empty workdays; doctor calendar stays independent", () => {
  assert.match(doctorsPage, /\/clinic\/doctor-laser-weekdays"\)\.catch\(\(\) => null\)/);
  assert.match(doctorsPage, /\/inventory\/laser-devices"\)\.catch\(\(\) => null\)/);
  assert.match(doctorsPage, /\/clinic\/doctor-admin"\)\.catch\(\(\) => null\)/);
  assert.match(doctorsPage, /adminDoctors === null/);
  assert.match(doctorsPage, /تعذر تحميل إدارة الدكاترة حالياً/);
  assert.match(doctorsUI, /laserRows === null \|\| laserDevices === null/);
  assert.match(doctorsUI, /role="alert"[\s\S]*?تعذر تحميل إعدادات أجهزة الليزر/);
  assert.match(doctorsUI, /لا يزال بإمكانك عرض الدكاترة/);
  assert.match(doctorsPage, /view === "month" \? "month" : "day"/);
  assert.match(doctorsPage, /\/booking\/doctor-calendar/);
  assert.match(doctorsPage, /view === "day" \? \(/);
});

test("doctor create, profile and schedule use visible success/error states without losing form inputs", () => {
  assert.match(doctorsUI, /useActionState<DoctorAdminState, FormData>\(createDoctorAction/);
  assert.match(doctorsUI, /useActionState<DoctorAdminState, FormData>\(updateDoctorAction/);
  assert.match(doctorsUI, /useActionState<DoctorAdminState, FormData>\(updateDoctorScheduleAction/);
  assert.match(doctorsUI, /role="alert" aria-live="polite"/);
  assert.match(doctorsUI, /role="status" aria-live="polite"/);
  assert.match(doctorsUI, /<ScheduleFields initialHours=\{doctor\.working_hours\} \/>/);
  assert.match(doctorsUI, /router\.refresh\(\)/);
  assert.doesNotMatch(doctorsUI, /\.reset\(\)/);
});

test("success is returned only after a successful doctor creation API response", async () => {
  const calls = [];
  revalidated.length = 0;
  api = async (url, options) => { calls.push({ url, options }); return { id: "doctor-1" }; };
  const result = await actions.createDoctorAction(initial, form([
    ["name", "Synthetic Doctor"],
    ["service_category", "laser"],
    ["intervals_json", JSON.stringify([{ weekday: 0, start_time: "09:00", end_time: "17:00" }])],
  ]));
  assert.equal(result.error, null);
  assert.match(result.notice, /تمت إضافة الدكتور/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/clinic/doctor-admin");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body).service_categories, ["laser"]);
  assert.ok(revalidated.includes("/doctors"));
});

test("failed doctor creation shows safe user-facing API message without false success", async () => {
  revalidated.length = 0;
  api = async () => { throw new FakeTiaApiError(500, "خدمة Linka غير متاحة حالياً.", "relation missing: doctor_laser_device_weekday_exclusions"); };
  const result = await actions.createDoctorAction(initial, form([
    ["name", "Synthetic Doctor"],
    ["intervals_json", "[]"],
  ]));
  assert.equal(result.notice, null);
  assert.equal(result.error, "خدمة Linka غير متاحة حالياً.");
  assert.doesNotMatch(result.error, /relation|database|doctor_laser_device_weekday_exclusions/i);
  assert.deepEqual(revalidated, []);
});

test("updating doctor schedule confirms the save and invalidates only after successful PUT", async () => {
  const calls = [];
  revalidated.length = 0;
  api = async (url, options) => { calls.push({ url, options }); return {}; };
  const result = await actions.updateDoctorScheduleAction(initial, form([
    ["doctor_id", "doc-1"],
    ["intervals_json", JSON.stringify([{ weekday: 5, start_time: "10:00", end_time: "15:00" }])],
  ]));
  assert.equal(result.error, null);
  assert.match(result.notice, /تم تحديث مواعيد عمل الدكتور/);
  assert.equal(calls[0].url, "/clinic/doctor-admin/doc-1/working-hours");
  assert.equal(calls[0].options.method, "PUT");
  // Preserve the open editor until its client refresh runs after showing success.
  assert.ok(!revalidated.includes("/doctors"));
  assert.ok(revalidated.includes("/appointments"));
});

test("laser device PUT persists per doctor/branch/weekday exclusions and confirms success", async () => {
  const calls = [];
  revalidated.length = 0;
  api = async (url, options) => { calls.push({ url, options }); return {}; };
  const data = form([
    ["doctor_id", "doc-1"],
    ["branch_id", "branch-2"],
    ["weekday", "5"],
    ["device_key", "candela"],
    ["device_key", "soprano"],
    ["allowed_device_key", "soprano"],
  ]);
  const result = await actions.saveDoctorLaserWeekday(initial, data);
  assert.equal(result.error, null);
  assert.match(result.notice, /تم حفظ أجهزة الليزر/);
  assert.equal(calls[0].url, "/clinic/doctor-laser-weekdays/doc-1/branch-2/5");
  assert.equal(calls[0].options.method, "PUT");
  assert.deepEqual(JSON.parse(calls[0].options.body), { excluded_device_keys: ["candela"] });
  assert.ok(revalidated.includes("/doctors"));
});

test("laser save fails closed with safe 403 and rejects invalid devices without any API request", async () => {
  revalidated.length = 0;
  let called = 0;
  api = async () => {
    called += 1;
    throw new FakeTiaApiError(403, "ليس لديك صلاحية لتنفيذ هذا الإجراء.", "private SQL/table details");
  };
  const good = form([
    ["doctor_id", "doc-1"], ["branch_id", "branch-1"], ["weekday", "0"],
    ["device_key", "candela"], ["allowed_device_key", "candela"],
  ]);
  const denied = await actions.saveDoctorLaserWeekday(initial, good);
  assert.equal(denied.notice, null);
  assert.equal(denied.error, "ليس لديك صلاحية لتنفيذ هذا الإجراء.");
  assert.doesNotMatch(denied.error, /private SQL|table details/);
  const invalid = await actions.saveDoctorLaserWeekday(initial, form([
    ["doctor_id", "doc-1"], ["branch_id", "branch-1"], ["weekday", "0"],
    ["device_key", "candela"], ["allowed_device_key", "unknown"],
  ]));
  assert.equal(invalid.notice, null);
  assert.match(invalid.error, /اختيارات الأجهزة غير صحيحة/);
  const missingWeekday = await actions.saveDoctorLaserWeekday(initial, form([
    ["doctor_id", "doc-1"], ["branch_id", "branch-1"],
    ["device_key", "candela"], ["allowed_device_key", "candela"],
  ]));
  assert.equal(missingWeekday.notice, null);
  assert.match(missingWeekday.error, /بيانات الدكتور أو اليوم غير صحيحة/);
  assert.equal(called, 1);
  assert.deepEqual(revalidated, []);
});


test("doctor profile update reports confirmed success and preserves the open editor contract", async () => {
  revalidated.length = 0;
  const calls = [];
  api = async (url, options) => { calls.push({ url, options }); return {}; };
  const result = await actions.updateDoctorAction(initial, form([
    ["doctor_id", "doc-1"], ["name", "Synthetic Doctor Updated"],
    ["booking_enabled", "on"], ["service_category", "laser"],
  ]));
  assert.equal(result.error, null);
  assert.match(result.notice, /تم تحديث بيانات الدكتور/);
  assert.equal(result.saved.doctor_id, "doc-1");
  assert.equal(result.saved.name, "Synthetic Doctor Updated");
  assert.equal(calls[0].url, "/clinic/doctor-admin/doc-1");
  assert.equal(calls[0].options.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[0].options.body).service_categories, ["laser"]);
  assert.doesNotMatch(doctorsUI, /<DoctorProfileForm[^>]*key=\{state/);
  assert.match(doctorsUI, /useRefreshAfterSuccess\(state\.notice\)/);
});

test("failed profile and schedule updates display safe errors and never claim success", async () => {
  revalidated.length = 0;
  let requests = 0;
  api = async () => {
    requests += 1;
    throw new FakeTiaApiError(409, "تعارض في البيانات. حدّث الصفحة وحاول مرة أخرى.", "SQL table metadata");
  };
  const profile = await actions.updateDoctorAction(initial, form([
    ["doctor_id", "doc-1"], ["name", "Synthetic Doctor"],
  ]));
  const schedule = await actions.updateDoctorScheduleAction(initial, form([
    ["doctor_id", "doc-1"], ["intervals_json", "[]"],
  ]));
  assert.equal(requests, 2);
  for (const result of [profile, schedule]) {
    assert.equal(result.notice, null);
    assert.match(result.error, /تعارض في البيانات/);
    assert.doesNotMatch(result.error, /SQL|metadata|relation/i);
  }
  assert.deepEqual(revalidated, []);
});
