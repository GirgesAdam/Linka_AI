import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { buildSchedulePeriods, isBookableFreePeriod } from "../src/app/(dashboard)/appointments/schedule-periods";

const root = resolve(__dirname, "..");
const page = readFileSync(resolve(root, "src/app/(dashboard)/appointments/page.tsx"), "utf8");
const form = readFileSync(resolve(root, "src/app/(dashboard)/appointments/manual-appointment-form.tsx"), "utf8");
const actions = readFileSync(resolve(root, "src/app/(dashboard)/appointments/actions.ts"), "utf8");

const day = "2026-10-09";
const interval = { weekday: 4, start_time: "09:00", end_time: "18:00" };
const appointment = (id: string, start: string, end: string) => ({
  id,
  start_at: `${day}T${start}:00Z`,
  end_at: `${day}T${end}:00Z`,
});
const block = (id: string, start: string, end: string) => ({
  id,
  start_at: `${day}T${start}:00Z`,
  end_at: `${day}T${end}:00Z`,
  scope: "all_services",
});

test("period builder uses appointments as dynamic boundaries", () => {
  const periods = buildSchedulePeriods(
    [appointment("a", "10:00", "10:30"), appointment("b", "12:00", "12:30")],
    interval,
    "UTC",
  );
  expect(periods.map((period) => ({
    start: period.start,
    end: period.end,
    kind: period.appointments.length ? "appointment" : "free",
  }))).toEqual([
    { start: 540, end: 600, kind: "free" },
    { start: 600, end: 630, kind: "appointment" },
    { start: 630, end: 720, kind: "free" },
    { start: 720, end: 750, kind: "appointment" },
    { start: 750, end: 1080, kind: "free" },
  ]);
});

test("adjacent appointments do not create a free period between them", () => {
  const periods = buildSchedulePeriods(
    [appointment("a", "10:00", "10:30"), appointment("b", "10:30", "11:00")],
    interval,
    "UTC",
  );
  const freePeriods = periods.filter((period) => period.appointments.length === 0 && !period.block);
  expect(freePeriods).toEqual(expect.arrayContaining([
    expect.objectContaining({ start: 540, end: 600 }),
    expect.objectContaining({ start: 660, end: 1080 }),
  ]));
  expect(freePeriods.some((period) => period.start >= 600 && period.end <= 660)).toBe(false);
});

test("availability blocks split a free period and remain non-bookable", () => {
  const periods = buildSchedulePeriods([], interval, "UTC", [block("closed", "12:00", "13:00")]);
  expect(periods.map((period) => ({
    start: period.start,
    end: period.end,
    blocked: Boolean(period.block),
  }))).toEqual([
    { start: 540, end: 720, blocked: false },
    { start: 720, end: 780, blocked: true },
    { start: 780, end: 1080, blocked: false },
  ]);
  expect(isBookableFreePeriod(periods[1])).toBe(false);
});

test("one free period maps to one booking entry point instead of slot-by-slot rows", () => {
  expect(page).not.toContain("bookingStartsForPeriod");
  expect(page).not.toContain("slotIntervalMinutes");
  expect(page).toContain("periods.map((period) => {");
  expect(page).toContain("period.start, period.end, preferredStart");
  expect(page).toContain("period.start,");
  expect(page).toContain("period.end,");
});

test("period click keeps the standard canonical booking flow and constrains slots to the period", () => {
  expect(page).toContain('query.set("book_start", String(periodStart))');
  expect(page).toContain('query.set("book_end", String(periodEnd))');
  expect(page).toContain("windowStartMinutes={inlineBookingWindow?.start}");
  expect(page).toContain("windowEndMinutes={inlineBookingWindow?.end}");
  expect(form).toContain("getManualAppointmentAvailability");
  expect(form).toContain('const applyWindow = date === bookingDate');
  expect(actions).toContain('allow_immediate: "true"');
  expect(actions).toContain("endMinute <= Number(input.windowEndMinutes)");
  expect(actions).toContain('const endpoint = bookingMode === "quick" ? "/booking/appointments/quick" : "/booking/appointments"');
});

test("inline preferred time is only accepted when canonical availability returns it", () => {
  expect(form).toContain("const preferredSlot = result.slots.find(");
  expect(form).toContain("minuteInTimezone(slot.start_at, result.timezone) === preferredStartMinutes");
  expect(form).toContain("setStartAt(preferredSlot.start_at)");
  expect(form).toContain("الميعاد ده مبقاش متاح. اختار ميعاد تاني.");
  expect(actions).toContain("الميعاد ده مبقاش متاح. اختار ميعاد تاني.");
});

test("today past periods do not expose a booking action while an active period remains context only", () => {
  expect(isBookableFreePeriod({ start: 600, end: 720, appointments: [], block: null }, 910)).toBe(false);
  expect(isBookableFreePeriod({ start: 840, end: 1020, appointments: [], block: null }, 910)).toBe(true);
  expect(page).toContain("minimumInlineStartMinutes={selectedDate === today ? currentMinute : undefined}");
  expect(page).toContain("period.start >= minimumInlineStartMinutes ? period.start : undefined");
});

test("resource context remains fixed to the clicked column", () => {
  expect(page).toContain('query.set("book_column", column)');
  expect(page).toContain("fixedLaserDeviceKey={inlineBookingColumn?.deviceKey}");
  expect(page).toContain("allowedOperationalCategory={inlineBookingColumn?.operationalCategory}");
});

test("top and inline booking stay on the displayed date while the form date remains editable", () => {
  expect(page).toContain("bookingDate={bookingDateForForm}");
  expect(page).toContain('name="booking_date" type="date" defaultValue={bookingDateForForm}');
  expect(page).toContain("bookingDateForForm === selectedDate");
  expect(form).toContain("const [selectedBookingDate, setSelectedBookingDate] = useState(bookingDate)");
  expect(form).toContain("setSelectedBookingDate(nextDate)");
  expect(form).toContain("loadAvailability(serviceId, requiresLaserDevice ? laserDeviceKey : \"\", nextDate, false)");
});

test("changing booking date invalidates stale availability and drops old period-window context", () => {
  expect(form).toContain("availabilityRequestRef.current = requestId");
  expect(form).toContain("if (availabilityRequestRef.current !== requestId) return");
  expect(form).toContain('setStartAt("")');
  expect(form).toContain('setDoctorId("")');
  expect(form).toContain("windowStartMinutes: applyWindow ? windowStartMinutes : undefined");
  expect(form).toContain("windowEndMinutes: applyWindow ? windowEndMinutes : undefined");
});

test("mobile period UI stays period-based and keeps readable resource labels", () => {
  expect(page).toContain('aria-label="mobile appointment agenda"');
  expect(page).toContain("كل فترة فاضية لها نقطة حجز واحدة");
  expect(page).toContain("التخصص / الجهاز");
  expect(page).toContain(">الكل</Link>");
  expect(page).not.toContain("?????? / ?????");
});
