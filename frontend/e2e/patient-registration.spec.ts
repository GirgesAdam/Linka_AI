import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { getActiveOffersForService, parseEgpAmountToMinor } from "../src/app/(dashboard)/patients/registration-utils";

const root = resolve(__dirname, "..");
const page = readFileSync(resolve(root, "src/app/(dashboard)/patients/page.tsx"), "utf8");
const form = readFileSync(resolve(root, "src/app/(dashboard)/patients/patient-registration-form.tsx"), "utf8");
const actions = readFileSync(resolve(root, "src/app/(dashboard)/patients/actions.ts"), "utf8");
const openingEditor = readFileSync(resolve(root, "src/app/(dashboard)/patients/patient-opening-data-editor.tsx"), "utf8");
const api = readFileSync(resolve(root, "src/lib/tia/api.ts"), "utf8");
const profilePage = readFileSync(resolve(root, "src/app/(dashboard)/patients/[patientId]/page.tsx"), "utf8");
const normalizedApi = api.replace(/\r\n/g, "\n");

test("package offers are service-first and only active offers for the chosen service are shown", () => {
  const offers = [
    { id: "a1", service_id: "service-a", is_active: true },
    { id: "a2", service_id: "service-a", is_active: false },
    { id: "b1", service_id: "service-b", is_active: true },
  ];
  expect(getActiveOffersForService(offers, "")).toEqual([]);
  expect(getActiveOffersForService(offers, "service-a")).toEqual([offers[0]]);
  expect(getActiveOffersForService(offers, "service-b")).toEqual([offers[2]]);
});

test("EGP amounts convert exactly to minor units for full, partial, and zero payments", () => {
  expect(parseEgpAmountToMinor("6000.00")).toBe(600_000);
  expect(parseEgpAmountToMinor("4000")).toBe(400_000);
  expect(parseEgpAmountToMinor("12.3")).toBe(1_230);
  expect(parseEgpAmountToMinor("0")).toBe(0);
  expect(() => parseEgpAmountToMinor("-1")).toThrow();
  expect(() => parseEgpAmountToMinor("1.234")).toThrow();
});

test("registration is opened from a modal and package/Pulse sections collapse independently", () => {
  expect(page).toContain("<PatientRegistrationForm");
  expect(form).toContain('role="dialog"');
  expect(form).toContain('aria-modal="true"');
  expect(form).toContain("setIsOpen(true)");
  expect(form).toContain("setIsOpen(false)");
  expect((form.match(/<details /g) || []).length).toBe(2);
  expect(form).toContain('name="initial_package_sessions_remaining"');
  expect(form).not.toContain("sessionsTotal");
  expect(form).toContain("max={selectedOffer.sessions_count}");
  expect(form).not.toContain('name="initial_package_sessions_purchased"');
  expect(form).toContain('name="initial_pulse_device_key"');
  expect(form).toContain('name="initial_pulses_remaining"');
  expect(form).toContain('name="customer_id"');
});


test("source-level duplicate phone contract maps only structured duplicate conflicts to a profile link", () => {
  expect(normalizedApi).toContain("public detail?: unknown");
  expect(normalizedApi).toContain("const detail = body?.detail");
  expect(normalizedApi).toContain("technicalMessage,\n      detail,\n    );");
  expect(actions).toContain("error.status !== 409");
  expect(actions).toContain('detail.code !== "patient_phone_exists"');
  expect(actions).toContain('typeof detail.patient_id !== "string"');
  expect(actions).toContain('typeof detail.patient_name !== "string"');
  expect(actions).toContain("\\u0631\\u0642\\u0645 \\u0627\\u0644\\u0645\\u0648\\u0628\\u0627\\u064a\\u0644 \\u062f\\u0647 \\u0645\\u0633\\u062c\\u0644 \\u0628\\u0627\\u0644\\u0641\\u0639\\u0644 \\u0644\\u0639\\u0645\\u064a\\u0644 \\u0645\\u0648\\u062c\\u0648\\u062f.");
  expect(form).toContain("state.existingPatient.name");
  expect(form).toContain('href={`/patients/${state.existingPatient.id}`}');
  expect(profilePage.length).toBeGreaterThan(0);
  expect(form).not.toContain("?phone=");
});


test("source-level existing-patient opening-data flow loads canonical values before explicit save", () => {
  expect(form).toContain("<PatientOpeningDataEditor");
  expect(form).toContain("state.existingPatient &&");
  expect(openingEditor).toContain("استكمال / تحديث بيانات العميل");
  expect(openingEditor).toContain("loadPatientOpeningDataAction(patientId)");
  expect(openingEditor).toContain("setCustomerId(data.customer_id || \"\")");
  expect(openingEditor).toContain("String(data.package.sessions_remaining)");
  expect(openingEditor).toContain("String(balance.opening_pulses)");
  expect(openingEditor).toContain("savePatientOpeningDataAction(patientId");
  expect(openingEditor).toContain("تم تحديث بيانات العميل.");
  expect(actions).toContain("`/crm/patients/${patientId}/opening-data`");
  expect(actions).toContain('{ method: "PUT", body: JSON.stringify(payload) }');
});

test("source-level opening-data editor cannot mutate accounting and leaves patient profile UI unchanged", () => {
  expect(openingEditor).not.toContain("amount_paid");
  expect(openingEditor).not.toContain("payment_method");
  expect(openingEditor).not.toContain("refund");
  expect(openingEditor).toContain("replaceableIdentity");
  expect(openingEditor).toContain("currentPackage.current_remaining");
  expect(profilePage).not.toContain("PatientOpeningDataEditor");
  expect(profilePage).not.toContain("opening-data");
  expect(profilePage).not.toContain("استكمال / تحديث بيانات العميل");
});
