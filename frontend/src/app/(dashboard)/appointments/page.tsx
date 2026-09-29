import Link from "next/link";
import { CalendarDays, ChevronLeft, ChevronRight, Plus, Search } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDateTime } from "@/lib/format";
import { appointmentLabels } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import type {
  Appointment,
  Doctor,
  Patient,
  PatientPackage,
  PulseBalance,
  Service,
  Staff,
} from "@/lib/types";
import { ManualAppointmentForm } from "./manual-appointment-form";
import { QuickAppointmentDialog } from "./quick-appointment-dialog";

type SearchParams = {
  patient_id?: string;
  date?: string;
  branch_id?: string;
  manual_phone?: string;
  quick_phone?: string;
  quick_column?: string;
  quick_start?: string;
  quick_end?: string;
  column?: string | string[];
};

type KnowledgeHour = {
  weekday: number;
  start_time: string;
  end_time: string;
};

type BookingKnowledge = {
  workspace_timezone: string;
  booking_settings: { slot_interval_minutes?: number } | null;
  branches: Array<{
    id: string;
    name: string;
    timezone: string | null;
    is_active: boolean;
    working_hours: KnowledgeHour[];
  }>;
  doctors: Array<{
    id: string;
    branches: Array<{ id: string; is_primary: boolean }>;
  }>;
  patients: Array<{ id: string; name: string; phone: string | null }>;
};

const scheduleStatuses = new Set([
  "pending",
  "confirmed",
  "checked_in",
  "in_progress",
  "completed",
]);

function normalizePhoneIdentity(value: string | null | undefined) {
  if (!value) return "";
  let normalized = value.trim().replace(/[\s().-]/g, "");
  if (normalized.startsWith("0020") && normalized.length === 14) normalized = `+${normalized.slice(2)}`;
  else if (normalized.startsWith("01") && normalized.length === 11) normalized = `+20${normalized.slice(1)}`;
  else if (normalized.startsWith("20") && normalized.length === 12) normalized = `+${normalized}`;
  return normalized;
}

function phoneSearchVariants(value: string) {
  const identity = normalizePhoneIdentity(value);
  const variants = new Set([value.trim(), identity]);
  if (identity.startsWith("+20") && identity.length === 13) {
    variants.add(`0${identity.slice(3)}`);
    variants.add(identity.slice(1));
    variants.add(`00${identity.slice(1)}`);
  }
  return [...variants].filter(Boolean);
}

async function findPatientByPhone(value: string) {
  if (!value.trim()) return null;
  const resultSets = await Promise.all(
    phoneSearchVariants(value).map((variant) =>
      tiaRequest<Patient[]>(`/crm/patients?q=${encodeURIComponent(variant)}&limit=20`).catch(() => []),
    ),
  );
  const identity = normalizePhoneIdentity(value);
  const candidates = [...new Map(resultSets.flat().map((patient) => [patient.id, patient])).values()];
  return candidates.find((patient) => normalizePhoneIdentity(patient.phone) === identity) || null;
}

function dateInTimezone(timezone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function addDays(value: string, days: number) {
  const date = new Date(`${value}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function weekdayFor(value: string) {
  const sundayBased = new Date(`${value}T12:00:00Z`).getUTCDay();
  return (sundayBased + 6) % 7;
}

function toMinutes(value: string) {
  const [hour, minute] = value.slice(0, 5).split(":").map(Number);
  return hour * 60 + minute;
}

function minuteInTimezone(value: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(value));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return Number(values.hour) * 60 + Number(values.minute);
}

function minuteLabel(total: number) {
  const hour = Math.floor(total / 60);
  const minute = total % 60;
  return new Intl.DateTimeFormat("ar-EG", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "UTC",
  }).format(new Date(Date.UTC(2026, 0, 1, hour, minute)));
}

function appointmentTime(value: string, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    timeZone: timezone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(value));
}

type ScheduleColumnId = "prime" | "candela" | "dermatology" | "slimming" | "quick" | "other";

const scheduleColumns: Array<{ id: ScheduleColumnId; label: string }> = [
  { id: "prime", label: "Prime" },
  { id: "candela", label: "Candela" },
  { id: "dermatology", label: "جلدية" },
  { id: "slimming", label: "تخسيس" },
  { id: "quick", label: "حجوزات سريعة" },
  { id: "other", label: "أخرى" },
];

function requestedColumns(value: SearchParams["column"]) {
  const raw = Array.isArray(value) ? value : value ? [value] : scheduleColumns.map((item) => item.id);
  const allowed = new Set(scheduleColumns.map((item) => item.id));
  const selected = raw.filter((item): item is ScheduleColumnId => allowed.has(item as ScheduleColumnId));
  return selected.length ? selected : scheduleColumns.map((item) => item.id);
}

function appointmentColumn(appointment: Appointment, serviceById: Map<string, Service>): ScheduleColumnId {
  if (appointment.is_quick_booking) return "quick";
  if (appointment.laser_device_key === "prime_lase") return "prime";
  if (appointment.laser_device_key === "candela_gentle") return "candela";
  const category = serviceById.get(appointment.service_id)?.operational_category;
  if (category === "dermatology") return "dermatology";
  if (category === "slimming") return "slimming";
  return "other";
}

function appointmentsForColumn(
  appointments: Appointment[],
  column: ScheduleColumnId,
  serviceById: Map<string, Service>,
) {
  if (column === "quick") {
    return appointments.filter((appointment) => appointment.is_quick_booking === true);
  }
  return appointments.filter(
    (appointment) =>
      appointment.is_quick_booking !== true &&
      appointmentColumn(appointment, serviceById) === column,
  );
}

function scheduleHref(current: SearchParams, date: string, branchId: string) {
  const query = new URLSearchParams({ date, branch_id: branchId });
  if (current.patient_id) query.set("patient_id", current.patient_id);
  const columns = Array.isArray(current.column) ? current.column : current.column ? [current.column] : [];
  columns.forEach((column) => query.append("column", column));
  return `/appointments?${query.toString()}`;
}

function quickBookingHref(
  current: SearchParams,
  date: string,
  branchId: string,
  column: ScheduleColumnId,
  start: number,
  end: number,
) {
  const query = new URLSearchParams({ date, branch_id: branchId });
  const columns = Array.isArray(current.column) ? current.column : current.column ? [current.column] : [];
  columns.forEach((value) => query.append("column", value));
  query.set("quick_column", column);
  query.set("quick_start", String(start));
  query.set("quick_end", String(end));
  return `/appointments?${query.toString()}`;
}

type SchedulePeriod = {
  start: number;
  end: number;
  appointments: Appointment[];
};

function buildSchedulePeriods(
  appointments: Appointment[],
  interval: KnowledgeHour,
  timezone: string,
) {
  const workStart = toMinutes(interval.start_time);
  const workEnd = toMinutes(interval.end_time);
  const bookings = appointments
    .map((appointment) => {
      const start = minuteInTimezone(appointment.start_at, timezone);
      let end = minuteInTimezone(appointment.end_at, timezone);
      if (end <= start) end += 24 * 60;
      return {
        appointment,
        start: Math.max(start, workStart),
        end: Math.min(end, workEnd),
      };
    })
    .filter((booking) => booking.end > booking.start)
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const periods: SchedulePeriod[] = [];
  let cursor = workStart;
  let index = 0;

  while (index < bookings.length) {
    const first = bookings[index];
    const busyStart = first.start;
    let busyEnd = first.end;
    const busyAppointments = [first.appointment];
    index += 1;

    while (index < bookings.length && bookings[index].start < busyEnd) {
      busyEnd = Math.max(busyEnd, bookings[index].end);
      busyAppointments.push(bookings[index].appointment);
      index += 1;
    }

    if (busyStart > cursor) {
      periods.push({ start: cursor, end: busyStart, appointments: [] });
    }

    periods.push({
      start: busyStart,
      end: busyEnd,
      appointments: busyAppointments,
    });
    cursor = Math.max(cursor, busyEnd);
  }

  if (cursor < workEnd) {
    periods.push({ start: cursor, end: workEnd, appointments: [] });
  }

  return periods;
}

function MobileAgenda({ appointments, timezone, patientNames, serviceById, visibleColumns }: {
  appointments: Appointment[];
  timezone: string;
  patientNames: Map<string, string>;
  serviceById: Map<string, Service>;
  visibleColumns: ScheduleColumnId[];
}) {
  const visible = appointments
    .filter((appointment) => visibleColumns.includes(appointmentColumn(appointment, serviceById)))
    .slice()
    .sort((a, b) => a.start_at.localeCompare(b.start_at));

  if (!visible.length) {
    return <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-10 text-center text-sm font-semibold text-slate-500">{"\u0644\u0627 \u062a\u0648\u062c\u062f \u0645\u0648\u0627\u0639\u064a\u062f \u0641\u064a \u0647\u0630\u0627 \u0627\u0644\u064a\u0648\u0645 \u0636\u0645\u0646 \u0627\u0644\u0645\u0648\u0627\u0631\u062f \u0627\u0644\u0645\u062e\u062a\u0627\u0631\u0629."}</div>;
  }

  return (
    <div className="space-y-2 lg:hidden" aria-label="mobile appointment agenda">
      {visible.map((appointment) => {
        const service = serviceById.get(appointment.service_id);
        const resource = scheduleColumns.find((item) => item.id === appointmentColumn(appointment, serviceById));
        return (
          <Link key={appointment.id} href={`/appointments/${appointment.id}`} className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3 rounded-2xl border border-slate-200 bg-white p-3 transition active:bg-slate-50">
            <div className="border-l border-slate-100 pl-3 text-left" dir="ltr">
              <div className="text-sm font-black text-slate-950">{appointmentTime(appointment.start_at, timezone)}</div>
              <div className="mt-1 text-[11px] font-semibold text-slate-400">{appointmentTime(appointment.end_at, timezone)}</div>
            </div>
            <div className="min-w-0">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-black text-slate-950">{patientNames.get(appointment.patient_id) || "\u0639\u0645\u064a\u0644"}</div>
                  <div className="mt-0.5 truncate text-xs font-semibold text-slate-600">{service?.name || "\u062e\u062f\u0645\u0629"}</div>
                </div>
                <StatusBadge domain="appointment" status={appointment.status} showIcon={false} className="shrink-0" />
              </div>
              <div className="mt-2 text-[11px] font-bold text-[var(--accent-strong)]">{resource?.label || "\u0645\u0648\u0639\u062f"}</div>
            </div>
          </Link>
        );
      })}
    </div>
  );
}

function DailySchedule({
  appointments,
  hours,
  timezone,
  patientNames,
  serviceById,
  visibleColumns,
  selectedDate,
  branchId,
  currentParams,
  allowQuickBooking,
}: {
  appointments: Appointment[];
  hours: KnowledgeHour[];
  timezone: string;
  patientNames: Map<string, string>;
  serviceById: Map<string, Service>;
  visibleColumns: ScheduleColumnId[];
  selectedDate: string;
  branchId: string;
  currentParams: SearchParams;
  allowQuickBooking: boolean;
}) {
  if (!hours.length) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-5 py-12 text-center">
        <CalendarDays className="mx-auto text-slate-400" size={28} />
        <div className="mt-3 font-black text-slate-900">العيادة مغلقة في اليوم ده</div>
        <div className="mt-1 text-sm text-[var(--muted)]">اختار يوم تاني لعرض جدول المواعيد.</div>
      </div>
    );
  }

  const rendered = new Set<string>();
  const columns = scheduleColumns.filter((column) => visibleColumns.includes(column.id));

  return (
    <>
      <MobileAgenda appointments={appointments} timezone={timezone} patientNames={patientNames} serviceById={serviceById} visibleColumns={visibleColumns} />
      <div className="hidden space-y-4 lg:block">
      {hours
        .slice()
        .sort((a, b) => a.start_time.localeCompare(b.start_time))
        .map((interval, intervalIndex) => {
          const start = toMinutes(interval.start_time);
          const end = toMinutes(interval.end_time);
          return (
            <div key={`${interval.start_time}-${interval.end_time}-${intervalIndex}`} className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
              <div className="border-b border-slate-200 bg-slate-50 px-4 py-2 text-xs font-bold text-slate-600">
                ساعات العمل: {minuteLabel(start)} – {minuteLabel(end)}
              </div>
              <div className="overflow-x-auto">
                <div className="grid min-w-max" style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(230px, 1fr))` }}>
                  {columns.map((column) => {
                    const columnAppointments = appointmentsForColumn(
                      appointments,
                      column.id,
                      serviceById,
                    );
                    const periods = buildSchedulePeriods(columnAppointments, interval, timezone);
                    periods.forEach((period) => period.appointments.forEach((appointment) => rendered.add(appointment.id)));

                    return (
                      <div key={column.id} className="w-[260px] border-l border-slate-200 first:border-l-0 lg:w-auto">
                        <div className="sticky top-0 z-10 border-b border-slate-200 bg-white px-3 py-3 text-center text-sm font-black text-slate-900">
                          <div className="flex items-center justify-center gap-2">
                            <span>
                              {column.label}
                              <span className="mr-2 text-[11px] font-bold text-slate-400">
                                {columnAppointments.length.toLocaleString("ar-EG")}
                              </span>
                            </span>
                            {allowQuickBooking && column.id === "quick" && (
                              <Link
                                href={quickBookingHref(currentParams, selectedDate, branchId, "quick", start, end)}
                                className="inline-flex items-center gap-1 rounded-full border border-teal-200 bg-teal-50 px-2.5 py-1 text-[11px] font-black text-teal-800 transition hover:bg-teal-100 focus:outline-none focus:ring-2 focus:ring-teal-300"
                              >
                                <Plus size={13} />
                                حجز سريع
                              </Link>
                            )}
                          </div>
                        </div>
                        <div className="divide-y divide-slate-100">
                          {periods.map((period, periodIndex) => {
                            const isAvailable = period.appointments.length === 0;
                            return (
                              <div key={`${column.id}-${period.start}-${period.end}-${periodIndex}`} className={isAvailable ? "bg-slate-50/50 p-2" : "bg-white p-2"}>
                                <div className="mb-2 flex items-center justify-between gap-2 text-[11px] font-bold">
                                  <span className="text-slate-700">{minuteLabel(period.start)} – {minuteLabel(period.end)}</span>
                                  {isAvailable && column.id !== "quick" && <span className="text-slate-400">متاح</span>}
                                  {isAvailable && column.id === "quick" && <span className="text-slate-400">لا توجد حجوزات سريعة</span>}
                                </div>
                                {isAvailable ? (
                                  <div className="group relative min-h-12 rounded-xl border border-dashed border-slate-200 bg-white/80">
                                    {allowQuickBooking && column.id !== "quick" && (
                                      <Link
                                        href={quickBookingHref(
                                          currentParams,
                                          selectedDate,
                                          branchId,
                                          column.id,
                                          period.start,
                                          period.end,
                                        )}
                                        aria-label={`إضافة موعد في الفترة من ${minuteLabel(period.start)} إلى ${minuteLabel(period.end)}`}
                                        className="absolute inset-0 grid place-items-center rounded-xl text-teal-700 outline-none transition hover:bg-teal-50/80 focus:bg-teal-50 focus:ring-2 focus:ring-teal-300"
                                      >
                                        <span className="grid size-8 place-items-center rounded-full border border-teal-200 bg-white shadow-sm opacity-60 transition group-hover:scale-105 group-hover:opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
                                          <Plus size={17} />
                                        </span>
                                      </Link>
                                    )}
                                  </div>
                                ) : (
                                  <div className="space-y-2">
                                    {period.appointments.map((appointment) => {
                                      const service = serviceById.get(appointment.service_id);
                                      return (
                                        <Link
                                          key={appointment.id}
                                          href={`/appointments/${appointment.id}`}
                                          className="block rounded-xl border border-slate-200 bg-white px-3 py-2.5 shadow-sm transition hover:border-teal-300 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-teal-300"
                                        >
                                          <div className="flex items-start justify-between gap-2">
                                            <div className="min-w-0">
                                              <div className="truncate text-sm font-black text-slate-950">{patientNames.get(appointment.patient_id) || "عميل"}</div>
                                              <div className="mt-0.5 truncate text-xs font-semibold text-slate-600">{service?.name || "خدمة"}</div>
                                            </div>
                                            <StatusBadge domain="appointment" status={appointment.status} showIcon={false} className="shrink-0" />
                                          </div>
                                          <div className="mt-2 text-xs font-bold text-teal-700">
                                            {appointmentTime(appointment.start_at, timezone)} – {appointmentTime(appointment.end_at, timezone)}
                                          </div>
                                        </Link>
                                      );
                                    })}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          );
        })}

      {appointments.some((appointment) => !rendered.has(appointment.id) && visibleColumns.includes(appointmentColumn(appointment, serviceById))) && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4">
          <div className="text-sm font-black text-amber-950">مواعيد خارج ساعات العمل الحالية</div>
          <div className="mt-2 grid gap-2 md:grid-cols-2">
            {appointments
              .filter((appointment) => !rendered.has(appointment.id) && visibleColumns.includes(appointmentColumn(appointment, serviceById)))
              .map((appointment) => (
                <Link key={appointment.id} href={`/appointments/${appointment.id}`} className="rounded-xl bg-white p-3 text-sm shadow-sm">
                  <div className="font-black">{patientNames.get(appointment.patient_id) || "عميل"}</div>
                  <div className="mt-1 text-xs text-slate-600">
                    {serviceById.get(appointment.service_id)?.name || "خدمة"} · {appointmentTime(appointment.start_at, timezone)} – {appointmentTime(appointment.end_at, timezone)}
                  </div>
                </Link>
              ))}
          </div>
        </div>
      )}
      </div>
    </>
  );
}

export default async function AppointmentsPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const raw = await searchParams;
  const patientId = raw.patient_id;
  const manualPhone = (raw.manual_phone || "").trim();
  const quickPhone = (raw.quick_phone || "").trim();
  const visibleColumns = requestedColumns(raw.column);
  const quickColumn = scheduleColumns.some((item) => item.id === raw.quick_column)
    ? (raw.quick_column as ScheduleColumnId)
    : null;
  const quickStart = Number(raw.quick_start);
  const quickEnd = Number(raw.quick_end);
  const quickWindow =
    quickColumn &&
    Number.isInteger(quickStart) &&
    Number.isInteger(quickEnd) &&
    quickStart >= 0 &&
    quickEnd > quickStart &&
    quickEnd <= 24 * 60
      ? { column: quickColumn, start: quickStart, end: quickEnd }
      : null;

  const [knowledge, services, doctors, staff] = await Promise.all([
    tiaRequest<BookingKnowledge>("/clinic/knowledge"),
    tiaRequest<Service[]>("/clinic/services"),
    tiaRequest<Doctor[]>("/clinic/doctors"),
    tiaRequest<Staff[]>("/clinic/staff"),
  ]);

  const activeBranches = knowledge.branches.filter((branch) => branch.is_active);
  const selectedBranch =
    activeBranches.find((branch) => branch.id === raw.branch_id) ||
    activeBranches[0] ||
    null;
  const timezone = selectedBranch?.timezone || knowledge.workspace_timezone || "Africa/Cairo";
  const today = dateInTimezone(timezone);
  const selectedDate = /^\d{4}-\d{2}-\d{2}$/.test(raw.date || "") ? raw.date! : today;
  const weekday = weekdayFor(selectedDate);
  const workingHours = (selectedBranch?.working_hours || []).filter((hour) => hour.weekday === weekday);

  const query = new URLSearchParams({
    limit: "200",
    scope: "all",
    date: selectedDate,
  });
  if (selectedBranch) query.set("branch_id", selectedBranch.id);
  if (patientId) query.set("patient_id", patientId);

  const [allAppointments, selectedPatient] = await Promise.all([
    tiaRequest<Appointment[]>(`/booking/appointments?${query.toString()}`),
    patientId ? tiaRequest<Patient>(`/crm/patients/${patientId}`).catch(() => null) : Promise.resolve(null),
  ]);
  const appointments = allAppointments.filter((appointment) => scheduleStatuses.has(appointment.status));

  const [manualPatient, quickPatient] = await Promise.all([
    findPatientByPhone(manualPhone),
    quickWindow ? findPatientByPhone(quickPhone) : Promise.resolve(null),
  ]);

  const [manualPackages, manualHistory, manualPulseBalances] = manualPatient
    ? await Promise.all([
        tiaRequest<PatientPackage[]>(`/booking/patients/${manualPatient.id}/packages?usable_only=true`).catch(() => []),
        tiaRequest<Appointment[]>(`/booking/appointments?patient_id=${manualPatient.id}&scope=all&limit=20`).catch(() => []),
        tiaRequest<PulseBalance[]>(`/booking/patients/${manualPatient.id}/pulse-balance`).catch(() => []),
      ])
    : [[], [], []];

  const [quickPackages, quickHistory, quickPulseBalances] = quickPatient
    ? await Promise.all([
        tiaRequest<PatientPackage[]>(`/booking/patients/${quickPatient.id}/packages?usable_only=true`).catch(() => []),
        tiaRequest<Appointment[]>(`/booking/appointments?patient_id=${quickPatient.id}&scope=all&limit=20`).catch(() => []),
        tiaRequest<PulseBalance[]>(`/booking/patients/${quickPatient.id}/pulse-balance`).catch(() => []),
      ])
    : [[], [], []];

  const defaultBranchId = selectedBranch?.id;

  const patientNames = new Map(knowledge.patients.map((patient) => [patient.id, patient.name]));
  if (selectedPatient) {
    patientNames.set(
      selectedPatient.id,
      `${selectedPatient.first_name} ${selectedPatient.last_name || ""}`.trim(),
    );
  }
  const serviceNames = new Map(services.map((service) => [service.id, service.name]));
  const serviceById = new Map(services.map((service) => [service.id, service]));
  const currentParams: SearchParams = {
    patient_id: patientId,
    date: selectedDate,
    branch_id: selectedBranch?.id,
    column: visibleColumns,
  };

  return (
    <>
      <PageHeader
        title="المواعيد"
        description={
          selectedPatient
            ? `جدول مواعيد ${patientNames.get(selectedPatient.id) || "العميل"} حسب اليوم.`
            : "جدول يومي واضح مبني على ساعات عمل العيادة."
        }
        action={
          selectedPatient ? (
            <Link href={`/patients/${selectedPatient.id}`} className="inline-flex items-center gap-1 text-sm font-bold text-teal-700 hover:text-teal-800">
              الرجوع إلى ملف العميل <ChevronLeft size={15} />
            </Link>
          ) : undefined
        }
      />

      {!selectedPatient && (
        <details open={Boolean(manualPhone)} className="mb-5 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
          <summary className="flex cursor-pointer items-center gap-2 px-4 py-3 text-sm font-black text-slate-900">
            <Plus size={17} /> إضافة موعد
          </summary>
          <div className="border-t border-slate-100 p-4">
            <form method="get" className="flex max-w-xl gap-2">
              <input type="hidden" name="date" value={selectedDate} />
              {selectedBranch && <input type="hidden" name="branch_id" value={selectedBranch.id} />}
              <label className="min-w-0 flex-1">
                <span className="mb-1.5 block text-xs font-bold text-slate-600">رقم هاتف العميل</span>
                <Input name="manual_phone" defaultValue={manualPhone} required maxLength={40} dir="ltr" placeholder="01xxxxxxxxx" />
              </label>
              <Button type="submit" variant="outline" className="mt-6"><Search size={16} /> بحث</Button>
            </form>

            {manualPhone && (
              <div className="mt-5 border-t border-slate-100 pt-5">
                {manualPatient && (
                  <div className="mb-4 rounded-2xl border border-slate-200 bg-slate-50 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <div className="text-sm font-black text-slate-950">الحجوزات السابقة لـ {manualPatient.first_name} {manualPatient.last_name || ""}</div>
                        <div className="mt-1 text-xs text-[var(--muted)]">آخر {Math.min(manualHistory.length, 20).toLocaleString("ar-EG")} موعد مسجل لهذا الرقم.</div>
                      </div>
                      <Link href={`/patients/${manualPatient.id}`} className="text-xs font-bold text-teal-700 hover:underline">فتح ملف العميل</Link>
                    </div>
                    {manualHistory.length ? (
                      <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                        {manualHistory.slice(0, 6).map((appointment) => (
                          <Link href={`/appointments/${appointment.id}`} key={appointment.id} className="rounded-xl bg-white px-3 py-2 text-xs transition hover:ring-1 hover:ring-teal-300">
                            <div className="font-black text-slate-900">{serviceNames.get(appointment.service_id) || "خدمة"}</div>
                            <div className="mt-1 text-teal-700">{formatDateTime(appointment.start_at)}</div>
                            <div className="mt-1 font-bold text-slate-700">{appointmentLabels[appointment.status] || appointment.status}</div>
                          </Link>
                        ))}
                      </div>
                    ) : <div className="mt-3 text-xs text-[var(--muted)]">لا توجد حجوزات سابقة لهذا العميل.</div>}
                  </div>
                )}

                <ManualAppointmentForm
                  mode={manualPatient ? "existing" : "new"}
                  phone={manualPhone}
                  bookingDate={selectedDate}
                  branchId={defaultBranchId || ""}
                  patientId={manualPatient?.id}
                  patientName={manualPatient ? `${manualPatient.first_name} ${manualPatient.last_name || ""}`.trim() : undefined}
                  services={services}
                  doctors={doctors}
                  staff={staff}
                  packages={manualPackages}
                  pulseBalances={manualPulseBalances}
                  timezone={timezone}
                />
              </div>
            )}
          </div>
        </details>
      )}

      <Card>
        <CardHeader className="gap-4 border-b border-slate-100">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>{selectedBranch?.name || "العيادة"}</CardTitle>
              <div className="mt-1 text-sm font-semibold text-[var(--muted)]">
                {new Intl.DateTimeFormat("ar-EG", { dateStyle: "full", timeZone: "UTC" }).format(new Date(`${selectedDate}T12:00:00Z`))}
              </div>
            </div>
            <div className="rounded-full bg-slate-100 px-3 py-1.5 text-xs font-black text-slate-700">
              {appointments.length.toLocaleString("ar-EG")} موعد
            </div>
          </div>

          <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
            <form method="get" className="grid min-w-0 flex-1 gap-2 sm:grid-cols-[minmax(150px,1fr)_minmax(150px,1fr)_auto]">
              {patientId && <input type="hidden" name="patient_id" value={patientId} />}
              {visibleColumns.map((column) => <input key={column} type="hidden" name="column" value={column} />)}
              <label className="text-xs font-bold text-slate-700">
                اليوم
                <Input name="date" type="date" defaultValue={selectedDate} className="mt-1.5" />
              </label>
              <label className="text-xs font-bold text-slate-700">
                الفرع
                <select name="branch_id" defaultValue={selectedBranch?.id || ""} className="form-control mt-1.5 h-10 min-h-10">
                  {activeBranches.map((branch) => <option key={branch.id} value={branch.id}>{branch.name}</option>)}
                </select>
              </label>
              <Button type="submit" className="sm:self-end">عرض اليوم</Button>
            </form>
            {selectedBranch && (
              <div className="flex items-center gap-1.5">
                <Link href={scheduleHref(currentParams, addDays(selectedDate, -1), selectedBranch.id)} className="inline-flex h-10 items-center gap-1 rounded-xl border border-slate-200 px-3 text-xs font-black text-slate-700 hover:bg-slate-50">
                  <ChevronRight size={15} /> السابق
                </Link>
                <Link href={scheduleHref(currentParams, today, selectedBranch.id)} className="inline-flex h-10 items-center rounded-xl border border-slate-200 px-3 text-xs font-black text-teal-700 hover:bg-teal-50">
                  اليوم
                </Link>
                <Link href={scheduleHref(currentParams, addDays(selectedDate, 1), selectedBranch.id)} className="inline-flex h-10 items-center gap-1 rounded-xl border border-slate-200 px-3 text-xs font-black text-slate-700 hover:bg-slate-50">
                  التالي <ChevronLeft size={15} />
                </Link>
              </div>
            )}

          <form method="get" className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
            <input type="hidden" name="date" value={selectedDate} />
            {selectedBranch && <input type="hidden" name="branch_id" value={selectedBranch.id} />}
            {patientId && <input type="hidden" name="patient_id" value={patientId} />}
            <span className="ml-1 text-xs font-black text-slate-600">الأعمدة الظاهرة:</span>
            {scheduleColumns.map((column) => (
              <label key={column.id} className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-bold text-slate-700">
                <input type="checkbox" name="column" value={column.id} defaultChecked={visibleColumns.includes(column.id)} />
                {column.label}
              </label>
            ))}
            <Button type="submit" size="sm" variant="outline">تطبيق</Button>
          </form>
          </div>
        </CardHeader>

        <CardContent className="p-3 sm:p-5">
          {selectedBranch ? (
            <DailySchedule
              appointments={appointments}
              hours={workingHours}
              timezone={timezone}
              patientNames={patientNames}
              serviceById={serviceById}
              visibleColumns={visibleColumns}
              selectedDate={selectedDate}
              branchId={selectedBranch.id}
              currentParams={currentParams}
              allowQuickBooking={!patientId}
            />
          ) : (
            <div className="py-12 text-center text-sm font-semibold text-[var(--muted)]">لا يوجد فرع نشط لعرض جدول المواعيد.</div>
          )}
        </CardContent>
      </Card>

      {quickWindow && selectedBranch && !patientId && (
        <QuickAppointmentDialog
          branchId={selectedBranch.id}
          bookingDate={selectedDate}
          column={quickWindow.column}
          windowStartMinutes={quickWindow.start}
          windowEndMinutes={quickWindow.end}
          phone={quickPhone}
          patient={quickPatient}
          history={quickHistory}
          packages={quickPackages}
          pulseBalances={quickPulseBalances}
          services={services}
          doctors={doctors}
          staff={staff}
          visibleColumns={visibleColumns}
          closeHref={scheduleHref(currentParams, selectedDate, selectedBranch.id)}
          timezone={timezone}
        />
      )}
    </>
  );
}
