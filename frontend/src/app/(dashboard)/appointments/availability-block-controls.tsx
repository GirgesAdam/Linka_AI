"use client";

import { useActionState, useMemo, useRef, useState } from "react";
import { Ban, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createAvailabilityBlock, type AvailabilityBlockActionState } from "./actions";

const initialState: AvailabilityBlockActionState = { ok: false, message: "" };
const timeOptions = Array.from({ length: 48 }, (_, index) => {
  const total = index * 30;
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
});

function zonedParts(timezone: string) {
  const values = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date()).map((part) => [part.type, part.value]),
  );
  return {
    date: `${values.year}-${values.month}-${values.day}`,
    minutes: Number(values.hour) * 60 + Number(values.minute),
  };
}

function addDays(value: string, days: number) {
  const date = new Date(`${value}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function defaultsForDate(requestedDate: string, timezone: string) {
  const now = zonedParts(timezone);
  let date = requestedDate < now.date ? now.date : requestedDate;
  let startMinutes = date === now.date ? (Math.floor(now.minutes / 30) + 1) * 30 : 0;
  // 23:30 cannot start a same-day block because there is no 24:00 end option.
  if (startMinutes >= 23 * 60 + 30) {
    date = addDays(now.date, 1);
    startMinutes = 0;
  }
  const start = timeOptions[startMinutes / 30] || "00:00";
  const end = timeOptions[startMinutes / 30 + 1] || "00:30";
  return { date, start, end };
}

type ServiceOption = { id: string; name: string; is_active: boolean };

export function AvailabilityBlockControls({ branchId, date, timezone, services }: {
  branchId: string;
  date: string;
  timezone: string;
  services: ServiceOption[];
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [state, action, pending] = useActionState(createAvailabilityBlock, initialState);
  const initial = useMemo(() => defaultsForDate(date, timezone), [date, timezone]);
  const [blockDate, setBlockDate] = useState(initial.date);
  const [startTime, setStartTime] = useState(initial.start);
  const [endTime, setEndTime] = useState(initial.end);
  const [scope, setScope] = useState<"all_services" | "selected_services">("all_services");
  const [selectedServices, setSelectedServices] = useState<string[]>([]);
  const activeServices = services.filter((service) => service.is_active);
  const now = zonedParts(timezone);
  const today = now.date;
  const minStartMinutes = blockDate === today ? (Math.floor(now.minutes / 30) + 1) * 30 : 0;
  const startMinutes = Number(startTime.slice(0, 2)) * 60 + Number(startTime.slice(3, 5));
  const invalidSelectedScope = scope === "selected_services" && selectedServices.length === 0;

  function updateDate(nextDate: string) {
    const next = defaultsForDate(nextDate, timezone);
    setBlockDate(next.date);
    setStartTime(next.start);
    setEndTime(next.end);
  }

  function updateStart(nextStart: string) {
    setStartTime(nextStart);
    const index = timeOptions.indexOf(nextStart);
    if (index >= 0 && timeOptions.indexOf(endTime) <= index) {
      setEndTime(timeOptions[index + 1] || endTime);
    }
  }

  return <>
    <Button type="button" variant="outline" onClick={() => dialogRef.current?.showModal()}><Ban size={16} /> قفل فترة</Button>
    <dialog ref={dialogRef} className="m-auto w-[min(92vw,32rem)] rounded-2xl border border-slate-200 bg-white p-0 shadow-2xl backdrop:bg-slate-950/30">
      <form action={action} className="p-5">
        <input type="hidden" name="branch_id" value={branchId} />
        <div className="flex items-start justify-between gap-3"><div><div className="text-base font-black text-slate-950">قفل فترة</div><div className="mt-1 text-xs font-semibold text-slate-500">اقفل كل الخدمات أو خدمات محددة للحجوزات العادية والـAI خلال الفترة.</div></div><button type="button" aria-label="إغلاق" onClick={() => dialogRef.current?.close()} className="rounded-lg p-2 text-slate-500 hover:bg-slate-100"><X size={17} /></button></div>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <label className="text-xs font-bold text-slate-700 sm:col-span-2">التاريخ<Input className="mt-1.5" name="date" type="date" min={today} value={blockDate} onChange={(event) => updateDate(event.target.value)} required /></label>
          <label className="text-xs font-bold text-slate-700">من<select className="form-control mt-1.5 h-10 min-h-10" name="start_time" value={startTime} onChange={(event) => updateStart(event.target.value)} required>{timeOptions.slice(0, -1).map((value, index) => <option key={value} value={value} disabled={blockDate === today && index * 30 < minStartMinutes}>{value}</option>)}</select></label>
          <label className="text-xs font-bold text-slate-700">إلى<select className="form-control mt-1.5 h-10 min-h-10" name="end_time" value={endTime} onChange={(event) => setEndTime(event.target.value)} required>{timeOptions.map((value) => { const minutes = Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5)); return <option key={value} value={value} disabled={minutes <= startMinutes}>{value}</option>; })}</select></label>
        </div>
        <fieldset className="mt-5 rounded-2xl border border-slate-200 p-4">
          <legend className="px-2 text-xs font-black text-slate-800">نطاق القفل</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-sm font-bold"><input type="radio" name="scope" value="all_services" checked={scope === "all_services"} onChange={() => setScope("all_services")} /> كل الخدمات</label>
            <label className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-sm font-bold"><input type="radio" name="scope" value="selected_services" checked={scope === "selected_services"} onChange={() => setScope("selected_services")} /> خدمات محددة</label>
          </div>
          {scope === "selected_services" && <div className="mt-3 max-h-48 space-y-2 overflow-y-auto rounded-xl bg-slate-50 p-3">{activeServices.map((service) => <label key={service.id} className="flex items-center gap-2 text-sm font-semibold text-slate-700"><input type="checkbox" name="service_ids" value={service.id} checked={selectedServices.includes(service.id)} onChange={(event) => setSelectedServices((current) => event.target.checked ? [...current, service.id] : current.filter((id) => id !== service.id))} />{service.name}</label>)}{!activeServices.length && <div className="text-xs font-bold text-amber-700">لا توجد خدمات فعالة للاختيار.</div>}</div>}
          {invalidSelectedScope && <div className="mt-2 text-xs font-bold text-rose-700">اختر خدمة واحدة على الأقل.</div>}
        </fieldset>
        <label className="mt-4 block text-xs font-bold text-slate-700">السبب <span className="font-semibold text-slate-400">(اختياري)</span><Input className="mt-1.5" name="reason" maxLength={500} /></label>
        {state.message && <div className={`mt-4 rounded-xl px-3 py-2 text-xs font-bold ${state.ok ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"}`}>{state.message}</div>}
        <div className="mt-5 flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => dialogRef.current?.close()}>إلغاء</Button><Button type="submit" disabled={pending || invalidSelectedScope}>{pending ? "جارٍ الحفظ..." : "قفل الفترة"}</Button></div>
      </form>
    </dialog>
  </>;
}
