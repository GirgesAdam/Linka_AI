"use client";

import { useActionState, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, Clock3, LoaderCircle, Plus, Save, Stethoscope, Trash2, UserPlus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { KnowledgeHour } from "@/lib/agent-knowledge-types";
import type { ClinicLaserDevice } from "@/lib/types";
import type { DoctorLaserWeekday } from "./doctor-laser-weekdays";

import {
  createDoctorAction,
  removeDoctorAction,
  type DoctorAdminState,
  updateDoctorAction,
  updateDoctorScheduleAction,
} from "./actions";

type BranchSchedule = { branch_id: string; branch_name: string; is_primary: boolean; working_hours: KnowledgeHour[] };

export type DoctorAdminItem = {
  id: string;
  staff_id: string;
  name: string;
  specialization: string | null;
  phone: string | null;
  booking_enabled: boolean;
  is_active: boolean;
  services: Array<{ id: string; name: string }>;
  service_categories: Array<"laser" | "dermatology" | "slimming">;
  working_hours: KnowledgeHour[];
  branch_schedules?: BranchSchedule[];
};

const initialDoctorAdminState: DoctorAdminState = { notice: null, error: null, saved: null };

const days = [
  { weekday: 5, label: "السبت" },
  { weekday: 6, label: "الأحد" },
  { weekday: 0, label: "الإثنين" },
  { weekday: 1, label: "الثلاثاء" },
  { weekday: 2, label: "الأربعاء" },
  { weekday: 3, label: "الخميس" },
  { weekday: 4, label: "الجمعة" },
] as const;

type EditableInterval = {
  weekday: number;
  start_time: string;
  end_time: string;
};

function shortTime(value: string) {
  return value.slice(0, 5);
}

function normalizeHours(hours: KnowledgeHour[]): EditableInterval[] {
  return hours
    .map((row) => ({ weekday: row.weekday, start_time: shortTime(row.start_time), end_time: shortTime(row.end_time) }))
    .sort((a, b) => a.weekday - b.weekday || a.start_time.localeCompare(b.start_time));
}

function ActionState({ state }: { state: DoctorAdminState }) {
  if (state.error) {
    return <div role="alert" aria-live="polite" className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs font-semibold text-red-800">{state.error}</div>;
  }
  if (state.notice) {
    return <div role="status" aria-live="polite" className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-800"><CheckCircle2 size={14} /> {state.notice}</div>;
  }
  return null;
}

export function useRefreshAfterSuccess(state: Pick<DoctorAdminState, "notice" | "error">) {
  const router = useRouter();
  const pendingRefreshes = useRef(new Set<number>());

  useEffect(() => {
    if (state.error || !state.notice) return;
    const timeout = window.setTimeout(() => {
      pendingRefreshes.current.delete(timeout);
      router.refresh();
    }, 1600);
    pendingRefreshes.current.add(timeout);
  }, [router, state]);

  useEffect(() => () => {
    for (const timeout of pendingRefreshes.current) window.clearTimeout(timeout);
    pendingRefreshes.current.clear();
  }, []);
}

export function ScheduleFields({ initialHours = [], devices, rows = [] }: { initialHours?: KnowledgeHour[]; devices?: ClinicLaserDevice[] | null; rows?: DoctorLaserWeekday[] }) {
  const [intervals, setIntervals] = useState<EditableInterval[]>(() => normalizeHours(initialHours));
  const activeDevices = (devices || []).filter((device) => device.is_active);
  const [overrides, setOverrides] = useState<Record<number, string[]>>({});
  const excluded: Record<number, string[]> = { ...Object.fromEntries(rows.map((row) => [row.weekday, row.excluded_device_keys])), ...overrides };
  const restrictions = [...new Set(intervals.map((interval) => interval.weekday))].map((weekday) => ({
    weekday, excluded_device_keys: (excluded[weekday] || []).filter((key) => activeDevices.some((device) => device.device_key === key)),
  }));
  const byDay = useMemo(() => {
    const result = new Map<number, Array<{ row: EditableInterval; index: number }>>();
    intervals.forEach((row, index) => {
      const values = result.get(row.weekday) || [];
      values.push({ row, index });
      result.set(row.weekday, values);
    });
    return result;
  }, [intervals]);

  function addInterval(weekday: number) {
    setIntervals((current) => [...current, { weekday, start_time: "09:00", end_time: "17:00" }]);
  }

  function updateInterval(index: number, key: "start_time" | "end_time", value: string) {
    setIntervals((current) => current.map((row, rowIndex) => rowIndex === index ? { ...row, [key]: value } : row));
  }

  function removeInterval(index: number) {
    setIntervals((current) => current.filter((_, rowIndex) => rowIndex !== index));
  }

  return (
    <div className="space-y-2 overflow-x-auto">
      {devices != null && <input type="hidden" name="laser_weekday_exclusions_json" value={JSON.stringify(restrictions)} />}
      <input type="hidden" name="intervals_json" value={JSON.stringify(intervals)} />
      {days.map((day) => {
        const dayIntervals = byDay.get(day.weekday) || [];
        return (
          <div key={day.weekday} className="grid gap-2 rounded-xl border border-slate-100 bg-slate-50/70 p-2.5 min-w-[820px] grid-cols-[80px_minmax(290px,1fr)_minmax(250px,1fr)] items-center">
            <div className="text-xs font-black text-slate-700">{day.label}</div>
            <div className="space-y-2">
              {dayIntervals.length ? dayIntervals.map(({ row, index }) => (
                <div key={`${day.weekday}-${index}`} className="flex flex-wrap items-center gap-2">
                  <input type="time" value={row.start_time} onChange={(event) => updateInterval(index, "start_time", event.target.value)} className="form-control h-9 min-h-9 w-[125px]" aria-label={`بداية ${day.label}`} />
                  <span className="text-xs text-slate-400">إلى</span>
                  <input type="time" value={row.end_time} onChange={(event) => updateInterval(index, "end_time", event.target.value)} className="form-control h-9 min-h-9 w-[125px]" aria-label={`نهاية ${day.label}`} />
                  <button type="button" onClick={() => removeInterval(index)} className="rounded-lg p-2 text-slate-400 transition hover:bg-red-50 hover:text-red-700" aria-label={`حذف فترة ${day.label}`}><Trash2 size={14} /></button>
                </div>
              )) : null}
              <Button type="button" size="sm" variant="ghost" onClick={() => addInterval(day.weekday)}><Plus size={13} /> فترة</Button>
            </div>
            <div className="flex flex-wrap gap-3">
              {dayIntervals.length > 0 && activeDevices.map((device) => (
                <label key={device.device_key} className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={!(excluded[day.weekday] || []).includes(device.device_key)}
                    onChange={(event) => {
                      const checked = event.target.checked;
                      setOverrides((current) => {
                        const next = new Set(current[day.weekday] || excluded[day.weekday] || []);
                        if (checked) next.delete(device.device_key); else next.add(device.device_key);
                        return { ...current, [day.weekday]: [...next] };
                      });
                    }} />
                  {device.name}
                </label>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

const categoryOptions = [
  { value: "laser", label: "ليزر" },
  { value: "dermatology", label: "جلدية" },
  { value: "slimming", label: "تخسيس" },
] as const;

function CategoryChecklist({ selectedCategories = [], onChange }: { selectedCategories?: string[]; onChange?: (value: string, checked: boolean) => void }) {
  const selected = new Set(selectedCategories);
  return (
    <div className="grid gap-2 rounded-xl border border-slate-200 bg-white p-3 sm:grid-cols-3">
      {categoryOptions.map((category) => (
        <label key={category.value} className="flex items-center gap-2 rounded-lg p-2 text-sm font-bold text-slate-700 hover:bg-slate-50">
          <input
            type="checkbox"
            name="service_category"
            value={category.value}
            defaultChecked={selected.has(category.value)}
            onChange={(event) => onChange?.(category.value, event.target.checked)}
          />
          <span>{category.label}</span>
        </label>
      ))}
    </div>
  );
}

function CreateDoctorForm({ laserDevices }: { laserDevices: ClinicLaserDevice[] | null }) {
  const [open, setOpen] = useState(false);
  const [laserSelected, setLaserSelected] = useState(false);
  const [state, action, pending] = useActionState<DoctorAdminState, FormData>(createDoctorAction, initialDoctorAdminState);
  return (
    <div className="rounded-2xl border border-teal-200 bg-teal-50/40 p-4">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="flex w-full cursor-pointer items-center gap-2 text-start font-black text-teal-950"
        aria-expanded={open}
      >
        <UserPlus size={17} /> إضافة دكتور
      </button>
      {open && (
        <form action={action} className="mt-5 space-y-5">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <label className="text-xs font-bold text-slate-700">الاسم<input name="name" maxLength={120} className="form-control mt-1.5 h-10 min-h-10" /></label>
          </div>
          <div><div className="mb-2 text-xs font-black text-slate-700">تصنيفات الخدمات التي يقدمها الدكتور</div><CategoryChecklist onChange={(value, checked) => { if (value === "laser") setLaserSelected(checked); }} /></div>
          <div><div className="mb-2 flex items-center gap-2 text-xs font-black text-slate-700"><Clock3 size={14} /> مواعيد العمل الأسبوعية</div><ScheduleFields devices={laserSelected ? laserDevices : null} /></div>
          <ActionState state={state} />
          <Button type="submit" disabled={pending}>{pending ? <LoaderCircle size={15} className="animate-spin" /> : <UserPlus size={15} />} إضافة الدكتور</Button>
        </form>
      )}
    </div>
  );
}

function DoctorProfileForm({ doctor }: { doctor: DoctorAdminItem }) {
  const [state, action, pending] = useActionState<DoctorAdminState, FormData>(updateDoctorAction, initialDoctorAdminState);
  useRefreshAfterSuccess(state);
  return (
    <form action={action} className="space-y-4">
      <input type="hidden" name="doctor_id" value={doctor.id} />
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <label className="text-xs font-bold text-slate-700">الاسم<input name="name" defaultValue={doctor.name} maxLength={120} className="form-control mt-1.5 h-10 min-h-10" /></label>
        <label className="flex items-center gap-2 self-end rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-xs font-bold text-slate-700"><input type="checkbox" name="booking_enabled" defaultChecked={doctor.booking_enabled} /> متاح للحجز</label>
      </div>
      <div>
        <div className="mb-2 text-xs font-black text-slate-700">تصنيفات الخدمات</div>
        <CategoryChecklist selectedCategories={doctor.service_categories} />
        <div className="mt-1 text-[11px] font-semibold text-slate-500">ممكن تختار أكثر من تصنيف، أو تسيبها بدون اختيار لو الدكتور غير متاح للحجز حاليًا.</div>
      </div>
      <ActionState state={state} />
      <Button type="submit" size="sm" disabled={pending}>{pending ? <LoaderCircle size={14} className="animate-spin" /> : <Save size={14} />} حفظ البيانات</Button>
    </form>
  );
}

function DoctorScheduleForm({ doctor, branch, laserRows, laserDevices }: { doctor: DoctorAdminItem; branch?: BranchSchedule; laserRows: DoctorLaserWeekday[] | null; laserDevices: ClinicLaserDevice[] | null }) {
  const [state, action, pending] = useActionState<DoctorAdminState, FormData>(updateDoctorScheduleAction, initialDoctorAdminState);
  useRefreshAfterSuccess(state);
  return (
    <form action={action} className="space-y-3 rounded-2xl border border-slate-200 bg-white p-3">
      <input type="hidden" name="doctor_id" value={doctor.id} />
      {branch && <input type="hidden" name="branch_id" value={branch.branch_id} />}
      <div className="flex items-center gap-2 text-sm font-black text-slate-900"><Clock3 size={15} /> {branch?.branch_name || "مواعيد العمل الأسبوعية"}</div>
      <ScheduleFields initialHours={branch?.working_hours || doctor.working_hours} devices={laserRows !== null && doctor.service_categories.includes("laser") ? laserDevices : null} rows={laserRows?.filter((row) => row.doctor_id === doctor.id && row.branch_id === branch?.branch_id) || []} />
      <ActionState state={state} />
      <Button type="submit" size="sm" variant="outline" disabled={pending}>{pending ? <LoaderCircle size={14} className="animate-spin" /> : <Save size={14} />} حفظ المواعيد</Button>
    </form>
  );
}

function RemoveDoctorForm({ doctor }: { doctor: DoctorAdminItem }) {
  const [state, action, pending] = useActionState<DoctorAdminState, FormData>(removeDoctorAction, initialDoctorAdminState);
  return (
    <form action={action} onSubmit={(event) => { if (!window.confirm(`إزالة ${doctor.name || "الدكتور"} من الحجز النشط؟ المواعيد التاريخية ستظل محفوظة.`)) event.preventDefault(); }} className="space-y-2 border-t border-red-100 pt-4">
      <input type="hidden" name="doctor_id" value={doctor.id} />
      <ActionState state={state} />
      <Button type="submit" size="sm" variant="danger" disabled={pending}>{pending ? <LoaderCircle size={14} className="animate-spin" /> : <Trash2 size={14} />} مسح الدكتور</Button>
      <div className="text-[11px] font-semibold text-slate-500">الحذف آمن: يمنع الحجوزات الجديدة ويحتفظ بالمواعيد القديمة والتقارير.</div>
    </form>
  );
}

function DoctorEditorCard({
  doctor, laserRows, laserDevices,
}: {
  doctor: DoctorAdminItem;
  laserRows: DoctorLaserWeekday[] | null;
  laserDevices: ClinicLaserDevice[] | null;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50/50 p-4">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="w-full cursor-pointer text-start text-sm font-black text-slate-950"
        aria-expanded={open}
      >
        {doctor.name || "دكتور"}
      </button>
      {open && (
        <div className="mt-5 space-y-5">
          <DoctorProfileForm doctor={doctor} />
          {doctor.branch_schedules?.length ? doctor.branch_schedules.map((branch) => (
            <DoctorScheduleForm key={branch.branch_id} doctor={doctor} branch={branch} laserRows={laserRows} laserDevices={laserDevices} />
          )) : <DoctorScheduleForm doctor={doctor} laserRows={null} laserDevices={null} />}
          <RemoveDoctorForm doctor={doctor} />
        </div>
      )}
    </div>
  );
}

export function DoctorManagementPanel({
  doctors, laserRows, laserDevices,
}: {
  doctors: DoctorAdminItem[];
  laserRows: DoctorLaserWeekday[] | null;
  laserDevices: ClinicLaserDevice[] | null;
}) {
  const activeDoctors = doctors.filter((doctor) => doctor.is_active);

  return (
    <Card className="mb-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Stethoscope size={18} /> إدارة الدكاترة</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <CreateDoctorForm laserDevices={laserDevices} />
        {laserRows === null || laserDevices === null ? (
          <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm font-semibold text-red-800">
            تعذر تحميل إعدادات أجهزة الليزر. لا يزال بإمكانك عرض الدكاترة وتعديل بياناتهم ومواعيدهم.
          </div>
        ) : null}
        {activeDoctors.map((doctor) => (
          <DoctorEditorCard key={doctor.id} doctor={doctor} laserRows={laserRows} laserDevices={laserDevices} />
        ))}
      </CardContent>
    </Card>
  );
}
