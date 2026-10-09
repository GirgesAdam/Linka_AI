import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(__dirname, "..");
const page = readFileSync(resolve(root, "src/app/(dashboard)/appointments/page.tsx"), "utf8");
const form = readFileSync(resolve(root, "src/app/(dashboard)/appointments/manual-appointment-form.tsx"), "utf8");
const actions = readFileSync(resolve(root, "src/app/(dashboard)/appointments/actions.ts"), "utf8");

test("top booking uses the selected appointments date and keeps it editable", () => {
  expect(page).toContain("bookingDate={bookingDateForForm}");
  expect(page).toContain('name="booking_date" type="date" defaultValue={bookingDateForForm}');
  expect(form).toContain("const [selectedBookingDate, setSelectedBookingDate] = useState(bookingDate)");
  expect(form).toContain('type="date"');
  expect(form).toContain("value={selectedBookingDate}");
  expect(form).toContain("setSelectedBookingDate(nextDate)");
});

test("changing booking date invalidates stale availability before reloading", () => {
  expect(form).toContain("availabilityRequestRef.current = requestId");
  expect(form).toContain("if (availabilityRequestRef.current !== requestId) return");
  expect(form).toContain('setStartAt("")');
  expect(form).toContain('setDoctorId("")');
  expect(form).toContain("loadAvailability(serviceId, requiresLaserDevice ? laserDeviceKey : \"\", nextDate, false)");
  expect(page).toContain("bookingDateForForm === selectedDate");
  expect(form).not.toContain("date: bookingDate,");
  expect(form).toContain("date,");
});

test("appointments date or resource navigation clears stale booking state", () => {
  expect(page).toContain('key={`patient-booking-${selectedBranch.id}-${bookingDateForForm}-${inlineBookingColumn?.id || "any"}-${inlineBookingStartMinutes ?? "none"}`}');
  expect(page).toContain('key={`manual-booking-${defaultBranchId || "none"}-${bookingDateForForm}-${manualPhone}-${inlineBookingColumn?.id || "any"}-${inlineBookingStartMinutes ?? "none"}`}');
});

test("desktop and mobile free schedule time opens the same standard booking flow", () => {
  expect(page).toContain("function manualBookingHref(");
  expect(page).toContain('book: "1"');
  expect(page).toContain('query.set("book_column", column)');
  expect(page).toContain('query.set("book_time", String(start))');
  expect(page).toContain('aria-label="inline appointment booking"');
  expect(page.match(/manualBookingHref\(currentParams, selectedDate, branchId, column\.id, start\)/g)?.length || 0).toBeGreaterThanOrEqual(2);
  expect(page).toContain("<ManualAppointmentForm");
});

test("inline preferred time is accepted only when canonical availability returns it", () => {
  expect(form).toContain("const preferredSlot = result.slots.find(");
  expect(form).toContain("minuteInTimezone(slot.start_at, result.timezone) === preferredStartMinutes");
  expect(form).toContain("setStartAt(preferredSlot.start_at)");
  expect(form).toContain("الميعاد ده مبقاش متاح. اختار ميعاد تاني.");
  expect(actions).toContain("الميعاد ده مبقاش متاح. اختار ميعاد تاني.");
});

test("inline resource context reuses existing device/category constraints without bypassing booking API", () => {
  expect(page).toContain("fixedLaserDeviceKey={inlineBookingColumn?.deviceKey}");
  expect(page).toContain("allowedOperationalCategory={inlineBookingColumn?.operationalCategory}");
  expect(form).toContain("getManualAppointmentAvailability");
  expect(actions).toContain('const endpoint = bookingMode === "quick" ? "/booking/appointments/quick" : "/booking/appointments"');
  expect(actions).toContain("start_at: manualStartToIso(startsAt, clinicTimezone)");
});
