import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "../src");
const setup = readFileSync(join(root, "app/(dashboard)/setup/setup-importer.tsx"), "utf8");
const types = readFileSync(join(root, "lib/clinic-setup-v2-types.ts"), "utf8");
const laser = readFileSync(join(root, "app/(dashboard)/doctors/doctor-laser-weekdays.tsx"), "utf8");

test("setup UI no longer offers doctor type or visiting options", () => {
  assert.match(setup, /title="الدكاترة"/);
  assert.doesNotMatch(setup, /key: "doctor_type"/);
  assert.doesNotMatch(setup, /value: "visiting"/);
  assert.doesNotMatch(setup, /title="زيارات الدكاترة الزائرين"/);
});
test("setup displays one weekly working-hours editor for all doctors", () => {
  assert.match(setup, /title="مواعيد عمل الدكاترة الأسبوعية"/);
  assert.match(setup, /key: "doctor_name"/);
  assert.match(setup, /key: "day"/);
  assert.doesNotMatch(setup, /draft\.visiting_windows/);
});
test("frontend doctor contract does not include a visiting type or dated windows", () => {
  assert.match(types, /doctor_type: "regular";/);
  assert.match(types, /weekly_hours: ClinicHour\[\]/);
  assert.doesNotMatch(types, /VisitingWindow/);
  assert.doesNotMatch(types, /visiting_windows/);
});
test("laser per-weekday device management is retained", () => {
  assert.match(laser, /excluded_device_keys/);
  assert.match(laser, /row\.weekday/);
  assert.match(laser, /saveDoctorLaserWeekday/);
  assert.doesNotMatch(laser, /visiting/i);
});
