import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { buildSchedulePeriods, isBookableFreePeriod } from "../src/app/(dashboard)/appointments/schedule-periods";

const root = resolve(__dirname, "..");
const page = readFileSync(resolve(root, "src/app/(dashboard)/appointments/page.tsx"), "utf8");
const dialog = readFileSync(resolve(root, "src/app/(dashboard)/appointments/quick-appointment-dialog.tsx"), "utf8");
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

test("availability blocks split a free period and expose no booking entry inside the block", () => {
  const periods = buildSchedulePeriods([], interval, "UTC", [block("closed", "12:00", "13:00")]);
  expect(periods.map((period) => ({ start: period.start, end: period.end, blocked: Boolean(period.block) }))).toEqual([
    { start: 540, end: 720, blocked: false },
    { start: 720, end: 780, blocked: true },
    { start: 780, end: 1080, blocked: false },
  ]);
  expect(isBookableFreePeriod(periods[1])).toBe(false);
  expect(page).toContain('isBlocked ? (');
  expect(page).toContain('column.id === "quick" || availabilityUnknown || !isBookableFreePeriod(period, minimumInlineStartMinutes)');
});

test("one free period maps to one popup entry point instead of slot-by-slot rows", () => {
  expect(page).not.toContain("bookingStartsForPeriod");
  expect(page).not.toContain("slotIntervalMinutes");
  expect(page).not.toContain("manualBookingHref");
  expect(page).toContain("periods.map((period) => {");
  expect(page).toContain("quickBookingHref(currentParams, selectedDate, branchId, column.id, period.start, period.end)");
  expect(page).toContain("{allowQuickBooking && inlineBookingColumns.length > 0 && (");
  expect(page).toContain('!allowQuickBooking || column.id === "quick" || availabilityUnknown || !isBookableFreePeriod(period, minimumInlineStartMinutes)');
  expect(page).toContain("period.start,");
  expect(page).toContain("period.end,");
});

test("normal free-period plus opens the historical dialog over the schedule", () => {
  expect(page).toContain('query.set("quick_column", column)');
  expect(page).toContain('query.set("quick_start", String(start))');
  expect(page).toContain('query.set("quick_end", String(end))');
  expect(page).toContain("quickColumn &&");
  expect(page).toContain("visibleColumns.includes(quickColumn.id)");
  expect(page).toContain("<QuickAppointmentDialog");
  expect(dialog).toContain('role="dialog"');
  expect(dialog).toContain('aria-modal="true"');
  expect(dialog).toContain('{column === "quick" ? "حجز سريع استثنائي" : "إضافة موعد"}');
  expect(dialog).toContain('{bookingDate} · الفترة {minuteLabel(windowStartMinutes)} – {minuteLabel(windowEndMinutes)}');
});

test("historical popup UX keeps backdrop, sticky header, close button, desktop modal, and mobile sheet", () => {
  expect(dialog).toContain("fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4");
  expect(dialog).toContain("absolute inset-0 bg-slate-950/35 backdrop-blur-[1px]");
  expect(dialog).toContain("max-h-[92vh] w-full overflow-y-auto rounded-t-3xl");
  expect(dialog).toContain("sm:max-w-3xl sm:rounded-3xl");
  expect(dialog).toContain("sticky top-0 z-20");
  expect(dialog).toContain("<X size={17} />");
  expect((dialog.match(/href={closeHref}/g) || []).length).toBeGreaterThanOrEqual(2);
});

test("normal resources stay standard while the exceptional quick column stays quick", () => {
  expect(dialog).toContain('schedulingMode={column === "quick" ? "quick" : "standard"}');
  expect(dialog).toContain('fixedLaserDeviceKey={column === "quick" ? undefined : fixedLaserDeviceKey}');
  expect(dialog).toContain('allowedOperationalCategory={column === "quick" ? undefined : allowedOperationalCategory}');
  expect(actions).toContain('const endpoint = bookingMode === "quick" ? "/booking/appointments/quick" : "/booking/appointments"');
  expect(dialog).toContain('{column === "quick" && (');
  expect(dialog).toContain("الحجز السريع يسمح بتسجيل الموعد حتى مع وجود تداخل زمني مقصود");
});

test("period bounds remain context and canonical availability only offers slots fully inside them", () => {
  expect(dialog).toContain("windowStartMinutes={windowStartMinutes}");
  expect(dialog).toContain("windowEndMinutes={windowEndMinutes}");
  expect(form).toContain("getManualAppointmentAvailability");
  expect(form).toContain("const applyWindow = date === bookingDate");
  expect(actions).toContain('allow_immediate: "true"');
  expect(actions).toContain("endMinute <= Number(input.windowEndMinutes)");
  expect(actions).toContain("startMinute >= Number(input.windowStartMinutes)");
});

test("phone search remains in the popup for existing and new patients", () => {
  expect(dialog).toContain('<form method="get" className="flex gap-2">');
  expect(dialog).toContain('name="quick_phone"');
  expect(dialog).toContain('name="quick_column" value={column}');
  expect(dialog).toContain('name="quick_start" value={windowStartMinutes}');
  expect(dialog).toContain('name="quick_end" value={windowEndMinutes}');
  expect(dialog).toContain("{phone && (");
  expect(dialog).toContain('mode={patient ? "existing" : "new"}');
  expect(dialog).toContain("الحجوزات السابقة لـ");
  expect(dialog).toContain("فتح ملف العميل");
  expect(dialog).toContain("لا توجد حجوزات سابقة لهذا العميل.");
});

test("device and operational-category context stay fixed to the clicked resource", () => {
  expect(page).toContain('fixedLaserDeviceKey={scheduleColumns.find((item) => item.id === quickWindow.column)?.deviceKey}');
  expect(page).toContain('allowedOperationalCategory={scheduleColumns.find((item) => item.id === quickWindow.column)?.operationalCategory}');
  expect(dialog).toContain('fixedLaserDeviceKey={column === "quick" ? undefined : fixedLaserDeviceKey}');
  expect(dialog).toContain('allowedOperationalCategory={column === "quick" ? undefined : allowedOperationalCategory}');
});

test("changing booking date clears stale slot state and drops the old period window", () => {
  expect(form).toContain("availabilityRequestRef.current = requestId");
  expect(form).toContain("if (availabilityRequestRef.current !== requestId) return");
  expect(form).toContain('setStartAt("")');
  expect(form).toContain('setDoctorId("")');
  expect(form).toContain("setSelectedBookingDate(nextDate)");
  expect(form).toContain("windowStartMinutes: applyWindow ? windowStartMinutes : undefined");
  expect(form).toContain("windowEndMinutes: applyWindow ? windowEndMinutes : undefined");
  expect(form).toContain('loadAvailability(serviceId, requiresLaserDevice ? laserDeviceKey : "", nextDate, false)');
});

test("today past periods stay non-bookable while an active period remains context only", () => {
  expect(isBookableFreePeriod({ start: 600, end: 720, appointments: [], block: null }, 910)).toBe(false);
  expect(isBookableFreePeriod({ start: 840, end: 1020, appointments: [], block: null }, 910)).toBe(true);
  expect(page).toContain("minimumInlineStartMinutes={selectedDate === today ? currentMinute : undefined}");
  expect(form).toContain("getManualAppointmentAvailability");
});

test("top booking date behavior remains separate and editable", () => {
  expect(page).toContain("bookingDate={bookingDateForForm}");
  expect(page).toContain('name="booking_date" type="date" defaultValue={bookingDateForForm}');
  expect(page).toContain("bookingDateForForm === selectedDate");
  expect(form).toContain("const [selectedBookingDate, setSelectedBookingDate] = useState(bookingDate)");
});

test("mobile stays period-based and opens the same bottom-sheet dialog", () => {
  expect(page).toContain('aria-label="mobile appointment agenda"');
  expect(page).toContain("كل فترة فاضية لها نقطة حجز واحدة");
  expect(page).toContain("التخصص / الجهاز");
  expect(page).toContain(">الكل</Link>");
  expect(page).not.toContain("?????? / ?????");
  expect(dialog).toContain("flex items-end justify-center p-0 sm:items-center sm:p-4");
  expect(dialog).toContain("rounded-t-3xl");
});
