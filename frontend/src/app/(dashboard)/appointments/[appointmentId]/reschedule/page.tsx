import Link from "next/link";
import { ArrowRight, CalendarClock } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { tiaRequest } from "@/lib/tia/api";
import type { Appointment, AppointmentOperationsDetail, AvailabilityResponse, ClinicLaserDevice, Service } from "@/lib/types";

import { rescheduleAppointment } from "../actions";

type KnowledgeHour = { weekday: number; start_time: string; end_time: string };
type BookingKnowledge = {
  branches: Array<{ id: string; working_hours: KnowledgeHour[] }>;
  patients: Array<{ id: string; name: string }>;
};

type Period = { start: number; end: number; appointments: Appointment[] };
type ScheduleColumnId = string;
type ScheduleColumn = { id: ScheduleColumnId; label: string };

const activeStatuses = new Set(["pending", "confirmed", "checked_in", "in_progress"]);
const scheduleStatuses = new Set(["pending", "confirmed", "checked_in", "in_progress", "completed"]);

function buildScheduleColumns(devices: ClinicLaserDevice[]): ScheduleColumn[] {
  return [
    ...devices
      .filter((device) => device.is_active)
      .map((device) => ({
        id: `device:${device.device_key}`,
        label: device.name,
      })),
    { id: "dermatology", label: "جلدية" },
    { id: "slimming", label: "تخسيس" },
    { id: "quick", label: "حجوزات سريعة" },
  ];
}

function appointmentColumn(
  appointment: Appointment,
  serviceById: Map<string, Service>,
): ScheduleColumnId {
  if (appointment.doctor_assignment_known === false) return "quick";
  if (appointment.laser_device_key) return `device:${appointment.laser_device_key}`;
  const category = serviceById.get(appointment.service_id)?.operational_category;
  if (category === "dermatology") return "dermatology";
  if (category === "slimming") return "slimming";
  return "quick";
}

function localDateKey(value: string, timeZone: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(value));
  const map = new Map(parts.map((part) => [part.type, part.value]));
  return `${map.get("year")}-${map.get("month")}-${map.get("day")}`;
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

function timeLabel(value: string, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    timeZone: timezone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(value));
}

function buildPeriods(appointments: Appointment[], interval: KnowledgeHour, timezone: string) {
  const start = toMinutes(interval.start_time);
  const end = toMinutes(interval.end_time);
  const busy = appointments
    .map((appointment) => {
      const appointmentStart = minuteInTimezone(appointment.start_at, timezone);
      let appointmentEnd = minuteInTimezone(appointment.end_at, timezone);
      if (appointmentEnd <= appointmentStart) appointmentEnd += 1440;
      return {
        appointment,
        start: Math.max(start, appointmentStart),
        end: Math.min(end, appointmentEnd),
      };
    })
    .filter((item) => item.end > item.start)
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const periods: Period[] = [];
  let cursor = start;
  let index = 0;
  while (index < busy.length) {
    const first = busy[index];
    const busyAppointments = [first.appointment];
    let busyEnd = first.end;
    if (first.start > cursor) periods.push({ start: cursor, end: first.start, appointments: [] });
    index += 1;
    while (index < busy.length && busy[index].start < busyEnd) {
      busyEnd = Math.max(busyEnd, busy[index].end);
      busyAppointments.push(busy[index].appointment);
      index += 1;
    }
    periods.push({ start: first.start, end: busyEnd, appointments: busyAppointments });
    cursor = Math.max(cursor, busyEnd);
  }
  if (cursor < end) periods.push({ start: cursor, end, appointments: [] });
  return periods;
}

export default async function RescheduleAppointmentPage({
  params,
  searchParams,
}: {
  params: Promise<{ appointmentId: string }>;
  searchParams: Promise<{ date?: string }>;
}) {
  const { appointmentId } = await params;
  const { date } = await searchParams;
  const detail = await tiaRequest<AppointmentOperationsDetail>(
    `/booking/appointments/${appointmentId}/operations`,
  );
  const selectedDate =
    date && date.length === 10
      ? date
      : localDateKey(detail.appointment.start_at, detail.timezone);

  const availabilityQuery = new URLSearchParams({
    branch_id: detail.appointment.branch_id,
    service_id: detail.appointment.service_id,
    doctor_id: detail.appointment.doctor_id,
    date: selectedDate,
    exclude_appointment_id: appointmentId,
    allow_immediate: "true",
  });
  if (detail.appointment.laser_device_key) {
    availabilityQuery.set("laser_device_key", detail.appointment.laser_device_key);
  }

  const appointmentsQuery = new URLSearchParams({
    branch_id: detail.appointment.branch_id,
    date: selectedDate,
    scope: "all",
    limit: "200",
  });

  const [availability, dayAppointments, knowledge, services, laserDevices] = await Promise.all([
    detail.allowed_actions.includes("reschedule")
      ? tiaRequest<AvailabilityResponse>(`/booking/availability?${availabilityQuery.toString()}`)
      : Promise.resolve({ date: selectedDate, timezone: detail.timezone, slots: [] }),
    tiaRequest<Appointment[]>(`/booking/appointments?${appointmentsQuery.toString()}`),
    tiaRequest<BookingKnowledge>("/clinic/knowledge"),
    tiaRequest<Service[]>("/clinic/services"),
    tiaRequest<ClinicLaserDevice[]>("/inventory/laser-devices").catch(() => []),
  ]);
  const scheduleColumns = buildScheduleColumns(laserDevices);

  const branch = knowledge.branches.find((item) => item.id === detail.appointment.branch_id);
  const hours = (branch?.working_hours || [])
    .filter((hour) => hour.weekday === weekdayFor(selectedDate))
    .sort((a, b) => a.start_time.localeCompare(b.start_time));
  const patientNames = new Map(knowledge.patients.map((patient) => [patient.id, patient.name]));
  const serviceById = new Map(services.map((service) => [service.id, service]));
  const targetColumn = appointmentColumn(detail.appointment, serviceById);
  const scheduleAppointments = dayAppointments.filter(
    (appointment) =>
      appointment.id !== appointmentId &&
      scheduleStatuses.has(appointment.status),
  );
  const busyAppointments = dayAppointments.filter(
    (appointment) =>
      appointment.id !== appointmentId &&
      activeStatuses.has(appointment.status) &&
      (
        appointment.doctor_id === detail.appointment.doctor_id ||
        (
          detail.appointment.laser_device_key &&
          appointment.laser_device_key === detail.appointment.laser_device_key
        )
      ),
  );

  return (
    <>
      <PageHeader
        title="تغيير الموعد"
        description={`${detail.patient.name} · ${detail.service.name} · ${detail.doctor.name}`}
        action={
          <Link href={`/appointments/${appointmentId}`} className={buttonVariants({ variant: "outline" })}>
            <ArrowRight size={15} /> الرجوع للموعد
          </Link>
        }
      />
      <Card>
        <CardHeader><CardTitle>اختار يوم ووقت جديد</CardTitle></CardHeader>
        <CardContent>
          <form method="GET" className="flex flex-wrap items-end gap-2">
            <label className="text-sm font-bold">
              التاريخ
              <input type="date" name="date" defaultValue={selectedDate} className="mt-2 block h-10 rounded-xl border border-[var(--border)] px-3 font-normal" />
            </label>
            <Button type="submit" variant="outline">عرض اليوم</Button>
          </form>

          {!detail.allowed_actions.includes("reschedule") ? (
            <div className="mt-6 rounded-xl bg-[var(--surface-2)] p-8 text-center text-sm text-[var(--muted)]">
              حالة الموعد الحالية لا تسمح بتغيير ميعاده.
            </div>
          ) : !hours.length ? (
            <div className="mt-6 rounded-xl bg-[var(--surface-2)] p-8 text-center text-sm text-[var(--muted)]">
              العيادة مغلقة في اليوم ده.
            </div>
          ) : (
            <div className="mt-5 space-y-4">
              <div className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] px-3 py-2 text-xs font-bold text-[var(--accent-strong)]">
                العمود المميز هو مكان الموعد الحالي. اختيار الميعاد الجديد متاح داخله فقط، بينما باقي الأعمدة تعرض جدول اليوم للمقارنة.
              </div>
              {hours.map((interval, intervalIndex) => (
                <div key={`${interval.start_time}-${intervalIndex}`} className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
                  <div className="border-b border-slate-200 bg-slate-50 px-4 py-2 text-xs font-black text-slate-600">
                    ساعات العمل: {minuteLabel(toMinutes(interval.start_time))} – {minuteLabel(toMinutes(interval.end_time))}
                  </div>
                  <div className="overflow-x-auto">
                    <div
                      className="grid min-w-max"
                      style={{ gridTemplateColumns: `repeat(${scheduleColumns.length}, minmax(230px, 1fr))` }}
                    >
                      {scheduleColumns.map((column) => {
                        const isTarget = column.id === targetColumn;
                        const columnAppointments = scheduleAppointments.filter(
                          (appointment) => appointmentColumn(appointment, serviceById) === column.id,
                        );
                        const periods = buildPeriods(
                          isTarget ? busyAppointments : columnAppointments,
                          interval,
                          availability.timezone,
                        );

                        return (
                          <div
                            key={column.id}
                            className={`w-[260px] border-l border-slate-200 first:border-l-0 lg:w-auto ${isTarget ? "bg-[var(--accent-soft)]" : ""}`}
                          >
                            <div className={`border-b px-3 py-3 text-center text-sm font-black ${isTarget ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-slate-900" : "border-slate-200 bg-white text-slate-900"}`}>
                              {column.label}
                              {isTarget && <span className="mr-2 rounded-full bg-[var(--accent-strong)] px-2 py-0.5 text-[10px] text-white">الموعد الحالي</span>}
                            </div>
                            <div className="divide-y divide-slate-100">
                              {periods.map((period, periodIndex) => {
                                const availableSlots = isTarget
                                  ? availability.slots.filter((slot) => {
                                      const minute = minuteInTimezone(slot.start_at, availability.timezone);
                                      return minute >= period.start && minute < period.end;
                                    })
                                  : [];
                                const busy = period.appointments.length > 0;

                                return (
                                  <div
                                    key={`${column.id}-${period.start}-${period.end}-${periodIndex}`}
                                    className={`p-2 ${busy ? "bg-white" : "bg-slate-50/50"}`}
                                  >
                                    <div className="mb-2 flex items-center justify-between gap-2 text-[11px] font-bold">
                                      <span className="text-slate-700">{minuteLabel(period.start)} – {minuteLabel(period.end)}</span>
                                      {isTarget && !busy && (
                                        <span className={availableSlots.length ? "text-emerald-700" : "text-slate-400"}>
                                          {availableSlots.length ? "متاح" : "غير متاح"}
                                        </span>
                                      )}
                                    </div>

                                    {busy ? (
                                      <div className="space-y-2">
                                        {period.appointments.map((appointment) => (
                                          <div key={appointment.id} className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs shadow-sm">
                                            <div className="font-black text-slate-900">
                                              {patientNames.get(appointment.patient_id) || "موعد محجوز"}
                                            </div>
                                            <div className="mt-1 text-slate-500">
                                              {serviceById.get(appointment.service_id)?.name || "خدمة"} · {timeLabel(appointment.start_at, availability.timezone)} – {timeLabel(appointment.end_at, availability.timezone)}
                                            </div>
                                          </div>
                                        ))}
                                      </div>
                                    ) : isTarget && availableSlots.length ? (
                                      <div className="grid gap-2">
                                        {availableSlots.map((slot) => (
                                          <form key={slot.start_at} action={rescheduleAppointment}>
                                            <input type="hidden" name="appointment_id" value={appointmentId} />
                                            <input type="hidden" name="start_at" value={slot.start_at} />
                                            <Button type="submit" size="sm" variant="outline" className="w-full justify-between bg-white">
                                              <span>{timeLabel(slot.start_at, availability.timezone)} – {timeLabel(slot.end_at, availability.timezone)}</span>
                                              <CalendarClock size={14} />
                                            </Button>
                                          </form>
                                        ))}
                                      </div>
                                    ) : (
                                      <div className="min-h-12 rounded-xl border border-dashed border-slate-200 bg-white/80" />
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
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </>
  );
}