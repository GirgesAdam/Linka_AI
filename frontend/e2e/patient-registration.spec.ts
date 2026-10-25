import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { getActiveOffersForService, parseEgpAmountToMinor } from "../src/app/(dashboard)/patients/registration-utils";

const root = resolve(__dirname, "..");
const page = readFileSync(resolve(root, "src/app/(dashboard)/patients/page.tsx"), "utf8");
const form = readFileSync(resolve(root, "src/app/(dashboard)/patients/patient-registration-form.tsx"), "utf8");

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
  expect(form).not.toContain('name="initial_package_sessions_purchased"');
  expect(form).toContain('name="initial_pulse_device_key"');
  expect(form).toContain('name="initial_pulses_remaining"');
  expect(form).toContain('name="customer_id"');
});
