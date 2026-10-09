"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { StatusBadge } from "@/components/ui/status-badge";
import type { DashboardAppointment } from "@/lib/types";

const terminalStatuses = new Set(["completed", "cancelled", "no_show"]);

function formatTime(value: string | number, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(value));
}

function CurrentTimeLine({ now, timezone }: { now: number; timezone: string }) {
  return (
    <div className="grid grid-cols-[64px_minmax(0,1fr)] items-center gap-3 py-2 sm:grid-cols-[82px_minmax(0,1fr)]">
      <span className="text-end text-[11px] font-semibold text-violet-700">{formatTime(now, timezone)}</span>
      <div className="flex items-center gap-2" aria-label={`الوقت الحالي ${formatTime(now, timezone)}`}>
        <span className="size-2 shrink-0 rounded-full bg-violet-600" />
        <span className="h-px flex-1 bg-violet-200" />
        <span className="text-[11px] font-bold text-violet-700">الآن</span>
        <span className="h-px flex-1 bg-violet-200" />
      </div>
    </div>
  );
}

function AppointmentRow({
  appointment,
  timezone,
  isNext,
}: {
  appointment: DashboardAppointment;
  timezone: string;
  isNext: boolean;
}) {
  const terminal = terminalStatuses.has(appointment.status);
  const cancelled = appointment.status === "cancelled" || appointment.status === "no_show";

  return (
    <div
      className={`group relative grid min-h-16 grid-cols-[64px_minmax(0,1fr)] gap-3 rounded-2xl px-2 py-3 transition sm:grid-cols-[82px_minmax(0,1fr)] sm:px-3 ${
        isNext
          ? "bg-violet-50 ring-1 ring-violet-100 hover:bg-violet-100/70"
          : "hover:bg-slate-50"
      } ${terminal && !isNext ? "opacity-70" : ""}`}
    >
      <Link href={`/appointments/${appointment.id}`} aria-label={`فتح موعد ${appointment.patient_name}`} className="absolute inset-0 rounded-2xl" />
      <div className={`pointer-events-none relative z-10 pt-0.5 text-end text-base font-semibold tabular-nums ${isNext ? "text-violet-800" : "text-slate-800"}`}>
        {formatTime(appointment.start_at, timezone)}
      </div>
      <div className="pointer-events-none relative z-10 min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {appointment.patient_id ? (
            <Link href={`/patients/${appointment.patient_id}`} className={`pointer-events-auto relative z-20 truncate text-[15px] font-semibold hover:text-violet-700 hover:underline hover:underline-offset-2 ${cancelled ? "text-slate-500" : "text-slate-950"}`}>
              {appointment.patient_name}
            </Link>
          ) : (
            <div className={`truncate text-[15px] font-semibold ${cancelled ? "text-slate-500" : "text-slate-950"}`}>
              {appointment.patient_name}
            </div>
          )}
          {isNext && (
            <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] font-bold text-violet-800">التالي</span>
          )}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-slate-500">
          <span>{appointment.service_name}</span>
          <span aria-hidden="true">·</span>
          <span>{appointment.doctor_name}</span>
        </div>
        <div className="mt-2">
          <StatusBadge domain="appointment" status={appointment.status} showIcon={false} />
        </div>
      </div>
    </div>
  );
}

export function TodayAgenda({
  appointments,
  timezone,
  nextAppointmentId,
}: {
  appointments: DashboardAppointment[];
  timezone: string;
  nextAppointmentId: string | null;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const indicatorIndex = useMemo(() => {
    const index = appointments.findIndex((appointment) => new Date(appointment.start_at).getTime() >= now);
    return index === -1 ? appointments.length : index;
  }, [appointments, now]);

  if (!appointments.length) {
    return (
      <div className="rounded-2xl bg-white px-5 py-10 text-center">
        <div className="text-sm font-semibold text-slate-800">مفيش مواعيد مسجلة لليوم</div>
        <p className="mt-1 text-xs text-slate-500">أي حجز جديد لليوم هيظهر هنا فور فتح الصفحة من جديد.</p>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-white p-2 sm:p-3">
      {appointments.map((appointment, index) => (
        <div key={appointment.id}>
          {index === indicatorIndex && <CurrentTimeLine now={now} timezone={timezone} />}
          <AppointmentRow
            appointment={appointment}
            timezone={timezone}
            isNext={appointment.id === nextAppointmentId}
          />
        </div>
      ))}
      {indicatorIndex === appointments.length && <CurrentTimeLine now={now} timezone={timezone} />}
      {!nextAppointmentId && (
        <div className="mx-2 mt-2 rounded-xl bg-slate-50 px-4 py-3 text-center text-xs font-medium text-slate-500">
          مفيش مواعيد متبقية النهارده
        </div>
      )}
    </div>
  );
}
