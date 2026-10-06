import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(__dirname, "..");
const controls = readFileSync(resolve(root, "src/app/(dashboard)/appointments/availability-block-controls.tsx"), "utf8");
const actions = readFileSync(resolve(root, "src/app/(dashboard)/appointments/actions.ts"), "utf8");
const page = readFileSync(resolve(root, "src/app/(dashboard)/appointments/page.tsx"), "utf8");

test("availability block controls use half-hour options and selected-service validation", () => {
  expect(controls).toContain("index * 30");
  expect(controls).toContain('name="start_time"');
  expect(controls).toContain('name="end_time"');
  expect(controls).not.toContain('type="time"');
  expect(controls).toContain('value="all_services"');
  expect(controls).toContain('value="selected_services"');
  expect(controls).toContain("services.filter((service) => service.is_active)");
  expect(controls).toContain("pending || invalidSelectedScope");
  expect(actions).toContain("const halfHour = /^(?:[01]\\d|2[0-3]):(?:00|30)$/");
  expect(actions).toContain('scope === "selected_services" && serviceIds.length === 0');
});

test("near-term default rounds to the next half-hour without a one-hour lead", () => {
  expect(controls).toContain("(Math.floor(now.minutes / 30) + 1) * 30");
  expect(controls).not.toContain("now.minutes + 60");
  expect(controls).not.toContain("60 * 60");
});

test("quick booking presentation remains available inside availability blocks", () => {
  expect(page).toContain("buildSchedulePeriods(appointmentsForColumn(appointments, column, serviceById), interval, timezone, [])");
  expect(page).toContain('allowQuickBooking && column.id !== "quick" && period.appointments.length === 0');
  expect(page).toContain("quickBookingHref(currentParams, selectedDate, branchId");
});

test("block labels distinguish all-services from selected-services without long lists", () => {
  expect(page).toContain('block.scope === "all_services"');
  expect(page).toContain('if (names.length <= 2) return names.join(" + ")');
  expect(page).toContain('names.length.toLocaleString("ar-EG")');
});
