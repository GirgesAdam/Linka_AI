import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  getActiveOffersForService,
  isCurrentDuplicateRegistration,
  parseEgpAmountToMinor,
} from "../src/app/(dashboard)/patients/registration-utils";
import { patientDisplayName } from "../src/app/(dashboard)/patients/patient-utils";

const root = resolve(__dirname, "..");
const page = readFileSync(resolve(root, "src/app/(dashboard)/patients/page.tsx"), "utf8");
const form = readFileSync(resolve(root, "src/app/(dashboard)/patients/patient-registration-form.tsx"), "utf8");
const actions = readFileSync(resolve(root, "src/app/(dashboard)/patients/actions.ts"), "utf8");
const openingEditor = readFileSync(resolve(root, "src/app/(dashboard)/patients/patient-opening-data-editor.tsx"), "utf8");
const profilePage = readFileSync(resolve(root, "src/app/(dashboard)/patients/[patientId]/page.tsx"), "utf8");
const profileEditor = readFileSync(resolve(root, "src/app/(dashboard)/patients/[patientId]/patient-profile-editor.tsx"), "utf8");
const api = readFileSync(resolve(root, "src/lib/tia/api.ts"), "utf8");
const normalizedApi = api.replace(/\r\n/g, "\n");

test("package offers remain service-first and active-only", () => {
  const offers = [
    { id: "a1", service_id: "service-a", is_active: true },
    { id: "a2", service_id: "service-a", is_active: false },
    { id: "b1", service_id: "service-b", is_active: true },
  ];
  expect(getActiveOffersForService(offers, "")).toEqual([]);
  expect(getActiveOffersForService(offers, "service-a")).toEqual([offers[0]]);
  expect(getActiveOffersForService(offers, "service-b")).toEqual([offers[2]]);
});

test("EGP amounts convert exactly to minor units per package", () => {
  expect(parseEgpAmountToMinor("6000.00")).toBe(600_000);
  expect(parseEgpAmountToMinor("4000")).toBe(400_000);
  expect(parseEgpAmountToMinor("12.3")).toBe(1_230);
  expect(parseEgpAmountToMinor("0")).toBe(0);
  expect(() => parseEgpAmountToMinor("-1")).toThrow();
  expect(() => parseEgpAmountToMinor("1.234")).toThrow();
});

test("legacy first and last names render as one canonical patient name", () => {
  expect(patientDisplayName({ first_name: "Mona", last_name: "Ali" })).toBe("Mona Ali");
  expect(patientDisplayName({ first_name: "منى علي حسن", last_name: null })).toBe("منى علي حسن");
});

test("patients toolbar uses search placeholder, بحث and تسجيل عميل together", () => {
  expect(page).toContain('placeholder="ابحث بالاسم أو رقم الهاتف"');
  expect(page).toContain("<Search");
  expect(page).toContain('type="submit" className="px-5">بحث</Button>');
  expect(page).not.toContain(">تطبيق</Button>");
  expect(form).toContain('open: "تسجيل عميل"');
  expect(form).not.toContain('open: "تسجيل عميل جديد"');
  expect(page).toContain("<PatientRegistrationForm");
});

test("registration uses one name field, ID copy, dynamic packages and preserves Pulse", () => {
  expect(form).toContain('name="name"');
  expect(form).toContain('name: "الاسم *"');
  expect(form).not.toContain("الاسم الأول");
  expect(form).not.toContain("اسم العائلة");
  expect(form).not.toContain("Customer ID");
  expect(form).toContain('id: "ID (اختياري)"');
  expect(form).toContain('name="initial_packages_json"');
  expect(form).toContain("packageRows.map");
  expect(form).toContain('addPackage: "إضافة باكيدج"');
  expect(form).toContain('removePackage: "حذف الباكيدج"');
  expect(form).toContain('amountPaid: "المبلغ المدفوع (جنيه)"');
  expect(form).toContain('paymentMethod: "طريقة الدفع"');
  expect(form).toContain('value="cash"');
  expect(form).toContain('value="visa"');
  expect(form).toContain('value="instapay"');
  expect(actions).toContain("initial_packages: initialPackages");
  expect(actions).toContain("first_name: name");
  expect(actions).toContain("last_name: null");
  expect(form).toContain('name="initial_pulse_device_key"');
  expect(form).toContain('name="initial_pulses_remaining"');
});

test("patient profile shows one identity card and one edit entry point", () => {
  expect(profilePage).toContain(">الاسم</span>");
  expect(profilePage).toContain(">رقم الهاتف</span>");
  expect(profilePage).toContain(">ID</span>");
  expect(profilePage).toContain(">مصدر العميل</span>");
  expect(profilePage).toContain("<PatientProfileEditor");
  expect(profilePage).not.toContain("Customer ID (optional)");
  expect(profilePage).not.toContain("Save Customer ID");
  expect(profileEditor).toContain(">تعديل</Button>");
  expect(profileEditor).toContain("name: initialName");
  expect(profileEditor).toContain("phone: initialPhone");
  expect(profileEditor).toContain("customerId: initialCustomerId");
  expect(profileEditor).toContain("source: initialSource");
  expect(profileEditor).toContain("updatePatientProfileAction(patientId, changes)");
  expect(profileEditor).toContain("function cancel()");
  expect(actions).toContain("payload.first_name = name");
  expect(actions).toContain("payload.last_name = null");
});

test("source-level duplicate phone contract maps only structured duplicate conflicts to a profile link", () => {
  expect(normalizedApi).toContain("public detail?: unknown");
  expect(actions).toContain("error.status !== 409");
  expect(actions).toContain('detail.code !== "patient_phone_exists"');
  expect(actions).toContain('typeof detail.patient_id !== "string"');
  expect(actions).toContain('typeof detail.patient_name !== "string"');
  expect(actions).toContain("رقم الموبايل ده مسجل بالفعل لعميل موجود.");
  expect(form).toContain("activeDuplicatePatient.name");
  expect(form).toContain('href={`/patients/${activeDuplicatePatient.id}`}');
  expect(form).not.toContain("?phone=");
});

test("existing-patient opening-data flow supports multiple packages without payment UI", () => {
  expect(form).toContain("<PatientOpeningDataEditor");
  expect(form).toContain("key={activeDuplicatePatient.id}");
  expect(form).toContain("patientId={activeDuplicatePatient.id}");
  expect(openingEditor).toContain("data.packages.map");
  expect(openingEditor).toContain("package_id: row.packageId");
  expect(openingEditor).toContain("packages: packagePayload");
  expect(openingEditor).toContain("إضافة باكيدج أخرى");
  expect(openingEditor).toContain("loadPatientOpeningDataAction(patientId)");
  expect(openingEditor).toContain("savePatientOpeningDataAction(patientId");
  expect(openingEditor).toContain("تم تحديث بيانات العميل.");
  expect(openingEditor).not.toContain("amount_paid");
  expect(openingEditor).not.toContain("payment_method");
  expect(openingEditor).not.toContain("refund");
  expect(profilePage).not.toContain("PatientOpeningDataEditor");
});

test("F1 current duplicate identity remains actionable only for the submitted phone", () => {
  expect(isCurrentDuplicateRegistration("01011111111", "0", "01011111111", "0")).toBe(true);
  expect(actions).toContain("duplicatePhone: phone");
  expect(actions).toContain("duplicateAttempt");
  expect(form).toContain("activeDuplicatePatient.name");
});

test("F2 changing the phone immediately deactivates the old duplicate identity", () => {
  expect(isCurrentDuplicateRegistration("01011111111", "0", "01022222222", "0")).toBe(false);
  expect(form).toContain("state.duplicatePhone");
  expect(form).toContain("state.duplicateAttempt");
  expect(form).toContain("activeDuplicatePatient ? state.error : null");
  expect(form).toContain("onChange={(event) => setPhone(event.target.value)}");
});

test("F3 changing back may reactivate only within the same modal attempt", () => {
  expect(isCurrentDuplicateRegistration("01011111111", "4", "01022222222", "4")).toBe(false);
  expect(isCurrentDuplicateRegistration("01011111111", "4", "01011111111", "4")).toBe(true);
});

test("F4 duplicate patient switch remounts editor on the new patient", () => {
  expect(form).toContain("key={activeDuplicatePatient.id}");
  expect(form).toContain("patientId={activeDuplicatePatient.id}");
  expect(form).not.toContain("patientId={state.existingPatient.id}");
});

test("F5 save after patient switch uses the mounted editor patientId", () => {
  expect(openingEditor).toContain("loadPatientOpeningDataAction(patientId)");
  expect(openingEditor).toContain("savePatientOpeningDataAction(patientId");
  expect(actions).toContain("`/crm/patients/${patientId}/opening-data`");
});

test("F6 close and reopen invalidates the prior duplicate modal attempt", () => {
  expect(isCurrentDuplicateRegistration("01011111111", "0", "01011111111", "1")).toBe(false);
  expect(form).toContain("setDuplicateAttempt((current) => current + 1)");
  expect(form).toContain("setPhone(\"\")");
  expect(form).toContain("onClick={closeRegistrationModal}");
  expect(form).toContain('name="duplicate_attempt"');
});

test("F7 normal new registration remains POST + redirect with no duplicate editor target", () => {
  expect(actions).toContain('tiaRequest<{ id: string }>("/crm/patients/register"');
  expect(actions).toContain('redirect("/patients/" + patient.id)');
  expect(form).toContain("activeDuplicatePatient &&");
  expect(isCurrentDuplicateRegistration(undefined, undefined, "01033333333", "2")).toBe(false);
});
