import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(__dirname, "..");
const controls = readFileSync(resolve(root, "src/app/(dashboard)/appointments/availability-block-controls.tsx"), "utf8");
const actions = readFileSync(resolve(root, "src/app/(dashboard)/appointments/actions.ts"), "utf8");
const page = readFileSync(resolve(root, "src/app/(dashboard)/appointments/page.tsx"), "utf8");

test("block controls derive half-hour choices from clinic working hours", () => {
  expect(controls).toContain("workingHours");
  expect(controls).toContain("Math.ceil(rawStart / 30) * 30");
  expect(controls).toContain("Math.floor(rawEnd / 30) * 30");
  expect(controls).toContain("(Math.floor(now.minutes / 30) + 1) * 30");
  expect(controls).toContain('name="start_time"');
  expect(controls).toContain('name="end_time"');
  expect(controls).not.toContain('type="time"');
  expect(controls).toContain("العيادة مغلقة في اليوم ده.");
  expect(controls).toContain("intervalEnd");
  expect(actions).toContain("const halfHour = /^(?:[01]\\d|2[0-3]):(?:00|30)$/");
});

test("new block UI is resource scoped and excludes quick booking", () => {
  expect(controls).toContain('value="all_services"');
  expect(controls).toContain('value="selected_resources"');
  expect(controls).toContain('name="target_keys"');
  expect(controls).toContain("كل التخصصات");
  expect(controls).toContain("تخصصات محددة");
  expect(controls).not.toContain('value="selected_services"');
  expect(page).toContain('scheduleColumns.filter((column) => column.id !== "quick")');
  expect(page).toContain('.filter((device) => device.is_active)');
  expect(page).toContain('id: `device:${device.device_key}`');
  expect(actions).toContain('scope === "selected_resources" && targetKeys.length === 0');
});

test("desktop and mobile expose exact-block reopen controls", () => {
  expect(controls).toContain("الفترات المقفولة");
  expect(controls).toContain("ReopenAvailabilityBlockButton");
  expect(page).toContain("ReopenAvailabilityBlockButton");
  expect(actions).toContain('method: "DELETE"');
  expect(actions).toContain('revalidatePath("/appointments")');
  expect(actions).toContain("تعذر فتح الفترة");
});

test("quick booking CTA exists only in the quick exception column", () => {
  expect(page).toContain('column.id === "quick"');
  expect(page).toContain("الحجز السريع الاستثنائي");
  expect(page).toContain("للحالات الضرورية فقط");
  expect(page).toContain("استثناء");
  expect(page).toContain('visibleColumns.includes("quick")');
  expect(page).not.toContain('allowQuickBooking && column.id !== "quick"');
  expect(page).not.toContain('quickBookingHref(currentParams, selectedDate, branchId, column.id');
});

test("resource block rendering never paints quick and does not mislabel legacy service scopes", () => {
  expect(page).toContain('if (column === "quick") return []');
  expect(page).toContain('block.scope === "selected_resources"');
  expect(page).toContain('block.target_keys.includes(column)');
  expect(page).toContain("قفل قديم:");
  expect(page).toContain("partial service scope must not be painted");
});
