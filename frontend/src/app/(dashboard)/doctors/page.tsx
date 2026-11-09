import { PermissionLink as Link } from "@/components/permission-link";
import { CalendarDays, ChevronLeft, ChevronRight, Clock3, Stethoscope, UserRound } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { appointmentLabels, toneForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import { cn } from "@/lib/utils";

import { DoctorManagementPanel, type DoctorAdminItem } from "./doctor-management";

type DoctorCalendarDoctor = {
  id: string;
  name: string;
  specialization: string | null;
};

type DoctorCalendarEvent = {
  appointment_id: string;
  doctor_id: string;
  doctor_name: string;
  patient_id: string;
  patient_name: string;
  service_id: string;
  service_name: string;
  status: string;
  start_at: string;
  end_at: string;
  laser_device_name: string | null;
};

type DoctorCalendar = {
  timezone: string;
  start_date: string;
  end_date: string;
  doctors: DoctorCalendarDoctor[];
  events: DoctorCalendarEvent[];
};

type SearchParams = {
  view?: string;
  date?: string;
  doctor_id?: string;
};

const weekdayLabels = ["السبت", "الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة"];

function dateParts(value: Date) {
  return {
    year: value.getUTCFullYear(),
    month: value.getUTCMonth() + 1,
    day: value.getUTCDate(),
  };
}

function dateKey(value: Date) {
  const { year, month, day } = dateParts(value);
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function parseDateKey(value: string | undefined) {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) || dateKey(parsed) !== value ? null : parsed;
}

function cairoToday() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Africa/Cairo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function addDays(value: Date, days: number) {
  const next = new Date(value);
  next.setUTCDate(next.getUTCDate() + days);
  return next;
}

function addMonths(value: Date, months: number) {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + months, 1));
}

function startOfMonth(value: Date) {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), 1));
}

function endOfMonth(value: Date) {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + 1, 0));
}

function displayDate(value: Date, options?: Intl.DateTimeFormatOptions) {
  return new Intl.DateTimeFormat("ar-EG", {
    timeZone: "UTC",
    ...(options || { weekday: "long", day: "numeric", month: "long", year: "numeric" }),
  }).format(value);
}

function eventDateKey(value: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(value));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function eventTime(value: string, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    timeZone: timezone,
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function hrefFor(params: SearchParams, patch: Partial<SearchParams>) {
  const next = { ...params, ...patch };
  const query = new URLSearchParams();
  if (next.view && next.view !== "day") query.set("view", next.view);
  if (next.date) query.set("date", next.date);
  if (next.doctor_id) query.set("doctor_id", next.doctor_id);
  const encoded = query.toString();
  return encoded ? `/doctors?${encoded}` : "/doctors";
}

export default async function DoctorsPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const raw = await searchParams;
  const view = raw.view === "month" ? "month" : "day";
  const selectedDateKey = parseDateKey(raw.date) ? raw.date! : cairoToday();
  const selectedDate = parseDateKey(selectedDateKey)!;
  const doctorId = raw.doctor_id || "";
  const ctx = await getAppContext();
  const isAdmin = ctx.workspace.role === "admin";

  const rangeStart = view === "month" ? startOfMonth(selectedDate) : selectedDate;
  const rangeEnd = view === "month" ? endOfMonth(selectedDate) : selectedDate;
  const query = new URLSearchParams({ start_date: dateKey(rangeStart), end_date: dateKey(rangeEnd) });
  if (doctorId) query.set("doctor_id", doctorId);

  const [calendar, adminDoctors] = await Promise.all([
    tiaRequest<DoctorCalendar>(`/booking/doctor-calendar?${query.toString()}`),
    isAdmin ? tiaRequest<DoctorAdminItem[]>("/clinic/doctor-admin") : Promise.resolve([]),
  ]);

  const selectedDoctor = calendar.doctors.find((doctor) => doctor.id === doctorId) || null;
  const currentParams: SearchParams = { view, date: selectedDateKey, doctor_id: doctorId || undefined };
  const previousDate = view === "month" ? addMonths(selectedDate, -1) : addDays(selectedDate, -1);
  const nextDate = view === "month" ? addMonths(selectedDate, 1) : addDays(selectedDate, 1);
  const title = view === "month"
    ? displayDate(selectedDate, { month: "long", year: "numeric" })
    : displayDate(selectedDate);

  const eventsByDay = new Map<string, DoctorCalendarEvent[]>();
  for (const event of calendar.events) {
    const key = eventDateKey(event.start_at, calendar.timezone);
    const list = eventsByDay.get(key) || [];
    list.push(event);
    eventsByDay.set(key, list);
  }

  const firstMonthDay = startOfMonth(selectedDate);
  const saturdayOffset = (firstMonthDay.getUTCDay() + 1) % 7;
  const calendarStart = addDays(firstMonthDay, -saturdayOffset);
  const monthCells = Array.from({ length: 42 }, (_, index) => addDays(calendarStart, index));

  return (
    <>
      <PageHeader
        title="جدول الدكاترة"
        description="تابع مواعيد كل دكتور يوميًا أو على مستوى الشهر، وافتح أي موعد مباشرة من الجدول."
      />

      {isAdmin && <DoctorManagementPanel doctors={adminDoctors} />}

      <Card className="mb-5">
        <CardContent className="flex flex-col gap-4 p-4 sm:p-4">
          <div className="flex flex-wrap items-center gap-2">
            <Link href={hrefFor(currentParams, { view: "day" })} className={buttonVariants({ variant: view === "day" ? "default" : "outline", size: "sm" })}><Clock3 size={15} /> يومي</Link>
            <Link href={hrefFor(currentParams, { view: "month" })} className={buttonVariants({ variant: view === "month" ? "default" : "outline", size: "sm" })}><CalendarDays size={15} /> شهري</Link>
            <span className="mx-1 hidden h-7 w-px bg-slate-200 sm:block" />
            <Link href={hrefFor(currentParams, { date: dateKey(previousDate) })} className={buttonVariants({ variant: "outline", size: "sm" })} aria-label="الفترة السابقة"><ChevronRight size={16} /></Link>
            <div className="min-w-[190px] text-center text-sm font-black text-slate-900">{title}</div>
            <Link href={hrefFor(currentParams, { date: dateKey(nextDate) })} className={buttonVariants({ variant: "outline", size: "sm" })} aria-label="الفترة التالية"><ChevronLeft size={16} /></Link>
            <Link href={hrefFor(currentParams, { date: cairoToday() })} className={buttonVariants({ variant: "ghost", size: "sm" })}>اليوم</Link>
          </div>

          <form method="get" className="flex flex-wrap items-end gap-2 border-t border-slate-100 pt-4">
            <input type="hidden" name="view" value={view} />
            <input type="hidden" name="date" value={selectedDateKey} />
            <label className="min-w-[220px] flex-1 text-xs font-bold text-slate-700 sm:max-w-sm">
              الدكتور
              <select name="doctor_id" defaultValue={doctorId} className="form-control mt-1.5 h-10 min-h-10">
                <option value="">كل الدكاترة</option>
                {calendar.doctors.map((doctor) => <option key={doctor.id} value={doctor.id}>{doctor.name}{doctor.specialization ? ` · ${doctor.specialization}` : ""}</option>)}
              </select>
            </label>
            <Button type="submit" variant="outline">تطبيق</Button>
            {selectedDoctor && <span className="pb-2 text-xs font-semibold text-teal-700">عرض مواعيد {selectedDoctor.name}</span>}
          </form>
        </CardContent>
      </Card>

      {view === "day" ? (
        <Card>
          <CardContent className="p-0 sm:p-0">
            {calendar.events.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-right text-sm">
                  <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-500">
                    <tr><th className="px-4 py-3 font-bold">الوقت</th><th className="px-4 py-3 font-bold">الدكتور</th><th className="px-4 py-3 font-bold">العميل</th><th className="px-4 py-3 font-bold">الخدمة</th><th className="px-4 py-3 font-bold">الحالة</th></tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {calendar.events.map((event) => (
                      <tr key={event.appointment_id} className="transition hover:bg-slate-50/80">
                        <td className="whitespace-nowrap px-4 py-3 font-black text-teal-800"><Link href={`/appointments/${event.appointment_id}`} className="hover:underline">{eventTime(event.start_at, calendar.timezone)} – {eventTime(event.end_at, calendar.timezone)}</Link></td>
                        <td className="px-4 py-3"><div className="font-bold text-slate-900">{event.doctor_name}</div></td>
                        <td className="px-4 py-3"><Link href={`/patients/${event.patient_id}`} className="font-bold text-slate-900 hover:text-teal-800 hover:underline">{event.patient_name}</Link></td>
                        <td className="px-4 py-3"><div className="font-semibold text-slate-800">{event.service_name}</div>{event.laser_device_name && <div className="mt-1 text-[11px] font-bold text-teal-700">{event.laser_device_name}</div>}</td>
                        <td className="px-4 py-3"><Badge tone={toneForStatus(event.status)}>{appointmentLabels[event.status] || event.status}</Badge></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="p-10 text-center"><Stethoscope className="mx-auto text-slate-300" size={32} /><div className="mt-3 font-black text-slate-900">لا توجد مواعيد في اليوم ده</div><div className="mt-1 text-sm text-slate-500">غيّر اليوم أو اختار دكتور تاني.</div></div>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0 sm:p-0">
            <div className="grid grid-cols-7 border-b border-slate-200 bg-slate-50 text-center text-[11px] font-black text-slate-500">
              {weekdayLabels.map((day) => <div key={day} className="border-l border-slate-100 px-1 py-2 last:border-l-0">{day}</div>)}
            </div>
            <div className="grid grid-cols-7">
              {monthCells.map((day) => {
                const key = dateKey(day);
                const inMonth = day.getUTCMonth() === selectedDate.getUTCMonth();
                const dayEvents = eventsByDay.get(key) || [];
                const isToday = key === cairoToday();
                return (
                  <div key={key} className={cn("min-h-32 border-b border-l border-slate-100 p-2 last:border-l-0", !inMonth && "bg-slate-50/60 text-slate-400")}>
                    <div className="flex items-center justify-between gap-1">
                      <Link href={hrefFor(currentParams, { view: "day", date: key })} className={cn("flex h-7 w-7 items-center justify-center rounded-full text-xs font-black", isToday && "bg-teal-700 text-white", !isToday && inMonth && "text-slate-800 hover:bg-teal-50 hover:text-teal-800")}>{day.getUTCDate().toLocaleString("ar-EG")}</Link>
                      {dayEvents.length > 0 && <span className="text-[10px] font-bold text-slate-400">{dayEvents.length.toLocaleString("ar-EG")}</span>}
                    </div>
                    <div className="mt-2 space-y-1">
                      {dayEvents.slice(0, 4).map((event) => (
                        <Link key={event.appointment_id} href={`/appointments/${event.appointment_id}`} className="block rounded-md border border-teal-100 bg-teal-50/70 px-1.5 py-1 text-[10px] leading-4 text-teal-950 transition hover:border-teal-300 hover:bg-teal-50">
                          <span className="font-black">{eventTime(event.start_at, calendar.timezone)}</span><span className="mx-1 text-teal-500">·</span><span className="font-bold">{event.doctor_name}</span><span className="block truncate text-slate-600">{event.service_name} · {event.patient_name}</span>
                        </Link>
                      ))}
                      {dayEvents.length > 4 && <Link href={hrefFor(currentParams, { view: "day", date: key })} className="block text-center text-[10px] font-black text-teal-700 hover:underline">+{(dayEvents.length - 4).toLocaleString("ar-EG")} مواعيد</Link>}
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      <div className="mt-4 flex items-center gap-2 text-xs text-slate-500"><UserRound size={14} /> اضغط على أي موعد لفتح تفاصيله، أو على اسم العميل لفتح ملفه.</div>
    </>
  );
}
