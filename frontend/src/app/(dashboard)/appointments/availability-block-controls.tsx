"use client";

import { useActionState, useMemo, useRef, useState } from "react";
import { Ban, ListFilter, RotateCcw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  createAvailabilityBlock,
  reopenAvailabilityBlock,
  type AvailabilityBlockActionState,
} from "./actions";

const initialState: AvailabilityBlockActionState = { ok: false, message: "" };

type WorkingHour = { weekday: number; start_time: string; end_time: string };
type ResourceTarget = { key: string; label: string };
type ServiceOption = { id: string; name: string; is_active: boolean };
type BlockOption = {
  id: string;
  start_at: string;
  end_at: string;
  scope: "all_services" | "selected_services" | "selected_resources";
  service_ids: string[];
  target_keys: string[];
  reason: string | null;
};

function zonedParts(timezone: string) {
  const values = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date()).map((part) => [part.type, part.value]),
  );
  return {
    date: `${values.year}-${values.month}-${values.day}`,
    minutes: Number(values.hour) * 60 + Number(values.minute),
  };
}

function weekdayFor(value: string) {
  const sundayBased = new Date(`${value}T12:00:00Z`).getUTCDay();
  return (sundayBased + 6) % 7;
}

function toMinutes(value: string) {
  const [hour, minute] = value.slice(0, 5).split(":").map(Number);
  return hour * 60 + minute;
}

function timeValue(total: number) {
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function intervalGrid(hour: WorkingHour) {
  const rawStart = toMinutes(hour.start_time);
  const rawEnd = toMinutes(hour.end_time);
  const start = Math.ceil(rawStart / 30) * 30;
  const end = Math.floor(rawEnd / 30) * 30;
  return { start, end };
}

function selectableStarts(date: string, timezone: string, workingHours: WorkingHour[]) {
  const now = zonedParts(timezone);
  const nextBoundary = (Math.floor(now.minutes / 30) + 1) * 30;
  return workingHours
    .filter((hour) => hour.weekday === weekdayFor(date))
    .flatMap((hour) => {
      const interval = intervalGrid(hour);
      const first = date === now.date ? Math.max(interval.start, nextBoundary) : interval.start;
      const values: Array<{ value: string; intervalEnd: number }> = [];
      for (let minute = first; minute + 30 <= interval.end; minute += 30) {
        values.push({ value: timeValue(minute), intervalEnd: interval.end });
      }
      return values;
    });
}

function blockTime(value: string, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    timeZone: timezone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(value));
}

function scopeLabel(block: BlockOption, targets: ResourceTarget[], services: ServiceOption[]) {
  if (block.scope === "all_services") return "كل التخصصات";
  if (block.scope === "selected_services") {
    const names = block.service_ids.map((id) => services.find((service) => service.id === id)?.name).filter(Boolean) as string[];
    if (names.length <= 2 && names.length) return `قفل قديم: ${names.join(" + ")}`;
    return `قفل قديم: ${block.service_ids.length.toLocaleString("ar-EG")} خدمات`;
  }
  const labels = block.target_keys.map((key) => targets.find((target) => target.key === key)?.label || key);
  if (labels.length <= 2) return labels.join(" + ");
  return `${labels.length.toLocaleString("ar-EG")} تخصصات`;
}

export function ReopenAvailabilityBlockButton({ blockId, compact = false }: { blockId: string; compact?: boolean }) {
  const [state, action, pending] = useActionState(reopenAvailabilityBlock, initialState);
  return (
    <div className="min-w-0">
      <form action={action}>
        <input type="hidden" name="block_id" value={blockId} />
        <Button type="submit" size="sm" variant="outline" disabled={pending} className={compact ? "h-8 px-2 text-[11px]" : undefined}>
          <RotateCcw size={13} /> {pending ? "جارٍ الفتح..." : "فتح الفترة"}
        </Button>
      </form>
      {state.message && !state.ok && <div role="alert" className="mt-1 max-w-48 text-[11px] font-bold text-rose-700">{state.message}</div>}
    </div>
  );
}

export function AvailabilityBlockControls({
  branchId,
  date,
  timezone,
  workingHours,
  targets,
  services,
  blocks,
}: {
  branchId: string;
  date: string;
  timezone: string;
  workingHours: WorkingHour[];
  targets: ResourceTarget[];
  services: ServiceOption[];
  blocks: BlockOption[];
}) {
  const createDialogRef = useRef<HTMLDialogElement>(null);
  const managerDialogRef = useRef<HTMLDialogElement>(null);
  const [state, action, pending] = useActionState(createAvailabilityBlock, initialState);
  const now = useMemo(() => zonedParts(timezone), [timezone]);
  const initialDate = date < now.date ? now.date : date;
  const [blockDate, setBlockDate] = useState(initialDate);
  const initialStarts = useMemo(() => selectableStarts(initialDate, timezone, workingHours), [initialDate, timezone, workingHours]);
  const [startTime, setStartTime] = useState(initialStarts[0]?.value || "");
  const [endTime, setEndTime] = useState(initialStarts[0] ? timeValue(toMinutes(initialStarts[0].value) + 30) : "");
  const [scope, setScope] = useState<"all_services" | "selected_resources">("all_services");
  const [selectedTargets, setSelectedTargets] = useState<string[]>([]);
  const starts = selectableStarts(blockDate, timezone, workingHours);
  const selectedStart = starts.find((item) => item.value === startTime) || starts[0] || null;
  const effectiveStart = selectedStart?.value || "";
  const endOptions: string[] = [];
  if (selectedStart) {
    for (let minute = toMinutes(selectedStart.value) + 30; minute <= selectedStart.intervalEnd; minute += 30) {
      endOptions.push(timeValue(minute));
    }
  }
  const effectiveEnd = endOptions.includes(endTime) ? endTime : endOptions[0] || "";
  const hoursForDay = workingHours.filter((hour) => hour.weekday === weekdayFor(blockDate));
  const closedDay = hoursForDay.length === 0;
  const noRemainingTime = !closedDay && starts.length === 0;
  const invalidSelectedScope = scope === "selected_resources" && selectedTargets.length === 0;
  const submitDisabled = pending || closedDay || noRemainingTime || !effectiveStart || !effectiveEnd || invalidSelectedScope;

  function updateDate(nextDate: string) {
    setBlockDate(nextDate);
    const options = selectableStarts(nextDate, timezone, workingHours);
    const nextStart = options[0]?.value || "";
    setStartTime(nextStart);
    setEndTime(nextStart ? timeValue(toMinutes(nextStart) + 30) : "");
  }

  function updateStart(nextStart: string) {
    setStartTime(nextStart);
    const option = starts.find((item) => item.value === nextStart);
    const nextEnd = option && toMinutes(nextStart) + 30 <= option.intervalEnd
      ? timeValue(toMinutes(nextStart) + 30)
      : "";
    setEndTime(nextEnd);
  }

  return (
    <>
      <Button type="button" variant="outline" onClick={() => createDialogRef.current?.showModal()}><Ban size={16} /> قفل فترة</Button>
      <Button type="button" variant="outline" onClick={() => managerDialogRef.current?.showModal()}>
        <ListFilter size={16} /> الفترات المقفولة{blocks.length ? ` (${blocks.length.toLocaleString("ar-EG")})` : ""}
      </Button>

      <dialog ref={createDialogRef} className="m-auto w-[min(92vw,34rem)] rounded-2xl border border-slate-200 bg-white p-0 shadow-2xl backdrop:bg-slate-950/30">
        <form action={action} className="p-5">
          <input type="hidden" name="branch_id" value={branchId} />
          <div className="flex items-start justify-between gap-3">
            <div><div className="text-base font-black text-slate-950">قفل فترة</div><div className="mt-1 text-xs font-semibold text-slate-500">اقفل تخصص أو جهاز للحجوزات العادية والـAI. الحجز السريع الاستثنائي يظل متاحًا.</div></div>
            <button type="button" aria-label="إغلاق" onClick={() => createDialogRef.current?.close()} className="rounded-lg p-2 text-slate-500 hover:bg-slate-100"><X size={17} /></button>
          </div>
          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            <label className="text-xs font-bold text-slate-700 sm:col-span-2">التاريخ<Input className="mt-1.5" name="date" type="date" min={now.date} value={blockDate} onChange={(event) => updateDate(event.target.value)} required /></label>
            {closedDay ? (
              <div className="sm:col-span-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-sm font-bold text-amber-900">العيادة مغلقة في اليوم ده.</div>
            ) : noRemainingTime ? (
              <div className="sm:col-span-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-sm font-bold text-amber-900">لا توجد نقطة نصف ساعة صالحة متبقية للقفل في اليوم ده.</div>
            ) : (
              <>
                <label className="text-xs font-bold text-slate-700">من<select className="form-control mt-1.5 h-10 min-h-10" name="start_time" value={effectiveStart} onChange={(event) => updateStart(event.target.value)} required>{starts.map((item) => <option key={`${item.value}-${item.intervalEnd}`} value={item.value}>{item.value}</option>)}</select></label>
                <label className="text-xs font-bold text-slate-700">إلى<select className="form-control mt-1.5 h-10 min-h-10" name="end_time" value={effectiveEnd} onChange={(event) => setEndTime(event.target.value)} required>{endOptions.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
              </>
            )}
          </div>
          <fieldset className="mt-5 rounded-2xl border border-slate-200 p-4">
            <legend className="px-2 text-xs font-black text-slate-800">نطاق القفل</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-sm font-bold"><input type="radio" name="scope" value="all_services" checked={scope === "all_services"} onChange={() => setScope("all_services")} /> كل التخصصات</label>
              <label className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-sm font-bold"><input type="radio" name="scope" value="selected_resources" checked={scope === "selected_resources"} onChange={() => setScope("selected_resources")} /> تخصصات محددة</label>
            </div>
            {scope === "selected_resources" && <div className="mt-3 max-h-52 space-y-2 overflow-y-auto rounded-xl bg-slate-50 p-3">{targets.map((target) => <label key={target.key} className="flex items-center gap-2 text-sm font-semibold text-slate-700"><input type="checkbox" name="target_keys" value={target.key} checked={selectedTargets.includes(target.key)} onChange={(event) => setSelectedTargets((current) => event.target.checked ? [...current, target.key] : current.filter((key) => key !== target.key))} />{target.label}</label>)}{!targets.length && <div className="text-xs font-bold text-amber-700">لا توجد تخصصات تشغيلية فعالة للاختيار.</div>}</div>}
            {invalidSelectedScope && <div className="mt-2 text-xs font-bold text-rose-700">اختر تخصص أو جهاز واحد على الأقل.</div>}
          </fieldset>
          <label className="mt-4 block text-xs font-bold text-slate-700">السبب <span className="font-semibold text-slate-400">(اختياري)</span><Input className="mt-1.5" name="reason" maxLength={500} /></label>
          {state.message && <div className={`mt-4 rounded-xl px-3 py-2 text-xs font-bold ${state.ok ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"}`}>{state.message}</div>}
          <div className="mt-5 flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => createDialogRef.current?.close()}>إلغاء</Button><Button type="submit" disabled={submitDisabled}>{pending ? "جارٍ الحفظ..." : "قفل الفترة"}</Button></div>
        </form>
      </dialog>

      <dialog ref={managerDialogRef} className="m-auto w-[min(92vw,36rem)] rounded-2xl border border-slate-200 bg-white p-0 shadow-2xl backdrop:bg-slate-950/30">
        <div className="p-5">
          <div className="flex items-start justify-between gap-3"><div><div className="text-base font-black text-slate-950">الفترات المقفولة</div><div className="mt-1 text-xs font-semibold text-slate-500">فترات اليوم والفرع الحالي. فتح فترة يحذف القفل ده فقط.</div></div><button type="button" aria-label="إغلاق" onClick={() => managerDialogRef.current?.close()} className="rounded-lg p-2 text-slate-500 hover:bg-slate-100"><X size={17} /></button></div>
          <div className="mt-4 space-y-3">
            {!blocks.length && <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-4 py-8 text-center text-sm font-semibold text-slate-500">لا توجد فترات مقفولة في اليوم ده.</div>}
            {blocks.map((block) => <div key={block.id} className="rounded-2xl border border-rose-200 bg-rose-50 p-3"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="text-sm font-black text-rose-950">{blockTime(block.start_at, timezone)} – {blockTime(block.end_at, timezone)}</div><div className="mt-1 text-xs font-black text-rose-700">{scopeLabel(block, targets, services)}</div>{block.reason && <div className="mt-1 text-xs font-semibold text-rose-800">{block.reason}</div>}</div><ReopenAvailabilityBlockButton blockId={block.id} /></div></div>)}
          </div>
        </div>
      </dialog>
    </>
  );
}
