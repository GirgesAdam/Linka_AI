"use client";

import Link from "next/link";
import { useActionState, useMemo, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, CheckCircle2, Download, FileSpreadsheet, LoaderCircle, Plus, RotateCcw, Save, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import type {
  ClinicSetupDraft,
  ClinicSetupDraftRow,
  ClinicSetupImportResult,
  ClinicSetupV2Snapshot,
} from "@/lib/clinic-setup-v2-types";
import { applyClinicSetupDraftAction, importClinicSetupWorkbookAction } from "./actions";
import type { ClinicSetupImportActionState } from "./actions";

const initialClinicSetupImportState: ClinicSetupImportActionState = { result: null, error: null };
const field = "form-control mt-1 outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-100";
const dayNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const emptyDraft = (): ClinicSetupDraft => ({
  clinic_profile: {},
  services: [],
  doctors: [],
  doctor_services: [],
  clinic_hours: [],
  doctor_hours: [],
  visiting_windows: [],
  booking_policy: {},
});

const labels: Record<string, string> = {
  clinic_profile: "بيانات العيادة",
  services: "الخدمات",
  doctors: "الدكاترة",
  doctor_services: "خدمات الدكاترة",
  clinic_hours: "مواعيد العيادة",
  doctor_hours: "مواعيد الدكاترة الثابتين",
  visiting_windows: "زيارات الدكاترة الزائرين",
  booking_policy: "سياسة الحجز",
};

function valueText(value: unknown) {
  return value === null || value === undefined ? "" : String(value);
}

function snapshotToDraft(setup: ClinicSetupV2Snapshot): ClinicSetupDraft {
  const serviceById = new Map(setup.services.map((service) => [service.id, service.name]));
  const doctorServices: ClinicSetupDraftRow[] = [];
  for (const doctor of setup.doctors) {
    for (const serviceId of doctor.service_ids) {
      doctorServices.push({ doctor_name: doctor.full_name, service_name: serviceById.get(serviceId) || "" });
    }
  }
  return {
    clinic_profile: {
      name: setup.clinic.name || "",
      phone: setup.clinic.phone || "",
      address: setup.clinic.address || "",
      city: setup.clinic.city || "",
    },
    services: setup.services.map((service) => ({
      name: service.name,
      category: service.category || "",
      duration_minutes: service.duration_minutes,
      price: service.price,
    })),
    doctors: setup.doctors.map((doctor) => ({
      full_name: doctor.full_name,
      doctor_type: doctor.doctor_type,
      specialization: doctor.specialization || "",
    })),
    doctor_services: doctorServices,
    clinic_hours: setup.clinic_hours.map((row) => ({
      day: dayNames[row.weekday] || "",
      start_time: row.start_time.slice(0, 5),
      end_time: row.end_time.slice(0, 5),
    })),
    doctor_hours: setup.doctors.flatMap((doctor) => doctor.weekly_hours.map((row) => ({
      doctor_name: doctor.full_name,
      day: dayNames[row.weekday] || "",
      start_time: row.start_time.slice(0, 5),
      end_time: row.end_time.slice(0, 5),
    }))),
    visiting_windows: setup.doctors.flatMap((doctor) => doctor.visiting_windows.map((window) => {
      const start = new Date(window.start_at);
      const end = new Date(window.end_at);
      const cairoDate = new Intl.DateTimeFormat("en-CA", { timeZone: "Africa/Cairo", year: "numeric", month: "2-digit", day: "2-digit" }).format(start);
      const cairoStart = new Intl.DateTimeFormat("en-GB", { timeZone: "Africa/Cairo", hour: "2-digit", minute: "2-digit", hour12: false }).format(start);
      const cairoEnd = new Intl.DateTimeFormat("en-GB", { timeZone: "Africa/Cairo", hour: "2-digit", minute: "2-digit", hour12: false }).format(end);
      return { doctor_name: doctor.full_name, date: cairoDate, start_time: cairoStart, end_time: cairoEnd };
    })),
    booking_policy: {
      slot_interval_minutes: setup.booking_policy.slot_interval_minutes,
      minimum_notice_minutes: setup.booking_policy.minimum_notice_minutes,
      booking_horizon_days: setup.booking_policy.booking_horizon_days,
      cancellation_notice_minutes: setup.booking_policy.cancellation_notice_minutes,
      allow_same_day_booking: setup.booking_policy.allow_same_day_booking,
      require_confirmation: setup.booking_policy.require_confirmation,
    },
  };
}

type TableEditorProps = {
  title: string;
  rows: ClinicSetupDraftRow[];
  columns: Array<{ key: string; label: string; type?: "text" | "number" | "time" | "date" | "select"; options?: Array<{ value: string; label: string }> }>;
  onChange: (rows: ClinicSetupDraftRow[]) => void;
};

function TableEditor({ title, rows, columns, onChange }: TableEditorProps) {
  const shownRows = rows.length ? rows : [{}];
  const update = (index: number, key: string, value: string) => {
    const next = shownRows.map((row) => ({ ...row }));
    next[index][key] = value;
    onChange(next);
  };
  const add = () => onChange([...shownRows, {}]);
  const remove = (index: number) => onChange(shownRows.filter((_, rowIndex) => rowIndex !== index));

  return (
    <section className="space-y-3 rounded-2xl border border-[var(--border)] bg-white p-4">
      <div className="flex items-center justify-between gap-3"><b>{title}</b><Button type="button" variant="outline" onClick={add}><Plus size={14} /> صف</Button></div>
      <div className="space-y-3">
        {shownRows.map((row, index) => (
          <div key={index} className="grid gap-2 rounded-xl bg-slate-50 p-3 md:grid-cols-2 xl:grid-cols-4">
            {columns.map((column) => (
              <label key={column.key} className="text-[11px] font-bold">
                {column.label}
                {column.type === "select" ? (
                  <select className={field} value={valueText(row[column.key])} onChange={(event) => update(index, column.key, event.target.value)}>
                    <option value=""></option>
                    {(column.options || []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                  </select>
                ) : (
                  <input
                    className={field}
                    type={column.type || "text"}
                    value={valueText(row[column.key])}
                    onChange={(event) => update(index, column.key, event.target.value)}
                  />
                )}
              </label>
            ))}
            <div className="flex items-end xl:col-span-4"><Button type="button" variant="ghost" onClick={() => remove(index)} className="text-xs text-red-700">حذف الصف</Button></div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function ClinicSetupImporter({ initialSetup }: { initialSetup: ClinicSetupV2Snapshot }) {
  const router = useRouter();
  const [draft, setDraft] = useState<ClinicSetupDraft>(() => emptyDraft());
  const [saveResult, setSaveResult] = useState<ClinicSetupImportResult | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, startSaving] = useTransition();
  const [dirty, setDirty] = useState(false);
  const [state, previewAction, previewPending] = useActionState(
    async (
      previous: ClinicSetupImportActionState,
      formData: FormData,
    ) => {
      const next = await importClinicSetupWorkbookAction(previous, formData);
      if (next.result) {
        setDraft(next.result.draft);
        setSaveResult(null);
        setSaveError(null);
        setDirty(true);
      }
      return next;
    },
    initialClinicSetupImportState,
  );

  const importedCells = useMemo(() => {
    const profile = Object.values(draft.clinic_profile).filter((value) => value !== null && value !== "").length;
    const policy = Object.values(draft.booking_policy).filter((value) => value !== null && value !== "").length;
    return profile + policy + draft.services.length + draft.doctors.length + draft.doctor_services.length + draft.clinic_hours.length + draft.doctor_hours.length + draft.visiting_windows.length;
  }, [draft]);

  const updateRows = (key: keyof Pick<ClinicSetupDraft, "services" | "doctors" | "doctor_services" | "clinic_hours" | "doctor_hours" | "visiting_windows">, rows: ClinicSetupDraftRow[]) => {
    setDraft((current) => ({ ...current, [key]: rows }));
    setSaveResult(null);
    setDirty(true);
  };

  const updateProfile = (key: string, value: string) => {
    setDraft((current) => ({ ...current, clinic_profile: { ...current.clinic_profile, [key]: value } }));
    setSaveResult(null);
    setDirty(true);
  };

  const updatePolicy = (key: string, value: string) => {
    setDraft((current) => ({ ...current, booking_policy: { ...current.booking_policy, [key]: value } }));
    setSaveResult(null);
    setDirty(true);
  };

  const saveDraft = () => {
    setSaveError(null);
    startSaving(async () => {
      try {
        const result = await applyClinicSetupDraftAction(draft);
        setSaveResult(result);
        setDirty(false);
        router.refresh();
      } catch (error) {
        setSaveError(error instanceof Error ? error.message : "تعذر حفظ إعدادات العيادة.");
      }
    });
  };

  const savedReady = saveResult?.snapshot.readiness.ready ?? initialSetup.readiness.ready;
  const canContinue = savedReady && !dirty;
  const missing = saveResult?.snapshot.readiness.missing || initialSetup.readiness.missing;

  return (
    <div className="space-y-5">
      <div className="space-y-4 rounded-2xl border border-teal-200 bg-teal-50/40 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex items-start gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-white text-teal-700"><FileSpreadsheet size={19} /></span>
            <div>
              <b className="text-base">1. ارفع Excel أو املأ الخانات يدويًا</b>
              <p className="mt-1 max-w-3xl text-xs leading-5 text-[var(--muted)]">
                الخانات تبدأ فاضية. رفع Excel يقرأ الملف فقط ويملأ الموجود؛ أي قيمة ناقصة تفضل فاضية لحد ما تكملها بنفسك. لا يتم حفظ شيء قبل ما تضغط «حفظ الإعدادات».
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={() => { setDraft(snapshotToDraft(initialSetup)); setSaveResult(null); setSaveError(null); setDirty(false); }}><RotateCcw size={15} /> تحميل البيانات المحفوظة</Button>
            <a href="/api/clinic-setup-template" className={buttonVariants({ variant: "outline" })}><Download size={16} /> تحميل القالب</a>
          </div>
        </div>

        <form action={previewAction} className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <label className="flex-1 text-xs font-bold">ملف Excel
            <input type="file" name="file" accept=".xlsx" required className="form-control mt-1" />
          </label>
          <Button disabled={previewPending}>{previewPending ? <><LoaderCircle size={16} className="animate-spin" /> جاري القراءة</> : "قراءة الملف وتعبئة الخانات"}</Button>
        </form>

        {state.error && <div className="rounded-xl bg-red-50 p-3 text-sm font-bold text-red-700"><TriangleAlert size={17} className="ml-1 inline" />{state.error}</div>}
        {state.result && (
          <div className="flex flex-wrap items-center gap-2 rounded-xl bg-white p-3 text-sm">
            <CheckCircle2 size={18} className="text-emerald-700" />
            <b>تمت قراءة الملف. راجع الخانات تحت قبل الحفظ.</b>
            <Badge tone="green">{state.result.recognized_sheets.length} sheets</Badge>
            <Badge>{importedCells} صف/قيمة مقروءة</Badge>
          </div>
        )}
      </div>

      <div className="space-y-4">
        <div className="rounded-2xl border border-[var(--border)] bg-white p-4">
          <b>بيانات العيادة</b>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="text-xs font-bold sm:col-span-2">اسم العيادة<input className={field} value={valueText(draft.clinic_profile.name)} onChange={(e) => updateProfile("name", e.target.value)} /></label>
            <label className="text-xs font-bold">رقم العيادة<input className={field} value={valueText(draft.clinic_profile.phone)} onChange={(e) => updateProfile("phone", e.target.value)} /></label>
            <label className="text-xs font-bold">المدينة<input className={field} value={valueText(draft.clinic_profile.city)} onChange={(e) => updateProfile("city", e.target.value)} /></label>
            <label className="text-xs font-bold sm:col-span-2">العنوان<input className={field} value={valueText(draft.clinic_profile.address)} onChange={(e) => updateProfile("address", e.target.value)} /></label>
          </div>
        </div>

        <TableEditor title="الخدمات" rows={draft.services} onChange={(rows) => updateRows("services", rows)} columns={[
          { key: "name", label: "الخدمة" }, { key: "category", label: "التصنيف" }, { key: "duration_minutes", label: "المدة بالدقائق", type: "number" }, { key: "price", label: "السعر بالجنيه", type: "number" },
        ]} />

        <TableEditor title="الدكاترة" rows={draft.doctors} onChange={(rows) => updateRows("doctors", rows)} columns={[
          { key: "full_name", label: "اسم الدكتور" },
          { key: "doctor_type", label: "النوع", type: "select", options: [{ value: "regular", label: "ثابت" }, { value: "visiting", label: "زائر" }] },
          { key: "specialization", label: "التخصص" },
        ]} />

        <TableEditor title="الخدمات التي يقدمها كل دكتور" rows={draft.doctor_services} onChange={(rows) => updateRows("doctor_services", rows)} columns={[
          { key: "doctor_name", label: "الدكتور" }, { key: "service_name", label: "الخدمة" },
        ]} />

        <TableEditor title="مواعيد عمل العيادة" rows={draft.clinic_hours} onChange={(rows) => updateRows("clinic_hours", rows)} columns={[
          { key: "day", label: "اليوم" }, { key: "start_time", label: "من", type: "time" }, { key: "end_time", label: "إلى", type: "time" },
        ]} />

        <TableEditor title="مواعيد الدكاترة الثابتين" rows={draft.doctor_hours} onChange={(rows) => updateRows("doctor_hours", rows)} columns={[
          { key: "doctor_name", label: "الدكتور" }, { key: "day", label: "اليوم" }, { key: "start_time", label: "من", type: "time" }, { key: "end_time", label: "إلى", type: "time" },
        ]} />

        <TableEditor title="زيارات الدكاترة الزائرين" rows={draft.visiting_windows} onChange={(rows) => updateRows("visiting_windows", rows)} columns={[
          { key: "doctor_name", label: "الدكتور" }, { key: "date", label: "التاريخ", type: "date" }, { key: "start_time", label: "من", type: "time" }, { key: "end_time", label: "إلى", type: "time" },
        ]} />

        <section className="rounded-2xl border border-[var(--border)] bg-white p-4">
          <b>سياسة الحجز</b>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[
              ["slot_interval_minutes", "تقسيم المواعيد بالدقائق"],
              ["minimum_notice_minutes", "أقل وقت قبل الحجز بالدقائق"],
              ["booking_horizon_days", "الحجز المسبق بالأيام"],
              ["cancellation_notice_minutes", "مهلة الإلغاء بالدقائق"],
            ].map(([key, label]) => <label key={key} className="text-xs font-bold">{label}<input type="number" className={field} value={valueText(draft.booking_policy[key])} onChange={(e) => updatePolicy(key, e.target.value)} /></label>)}
            {[
              ["allow_same_day_booking", "الحجز في نفس اليوم"],
              ["require_confirmation", "الحجز يحتاج تأكيد"],
            ].map(([key, label]) => (
              <label key={key} className="text-xs font-bold">{label}
                <select className={field} value={valueText(draft.booking_policy[key])} onChange={(e) => updatePolicy(key, e.target.value)}>
                  <option value=""></option><option value="true">نعم</option><option value="false">لا</option>
                </select>
              </label>
            ))}
          </div>
        </section>
      </div>

      {saveError && <div className="rounded-xl bg-red-50 p-3 text-sm font-bold text-red-700"><TriangleAlert size={17} className="ml-1 inline" />{saveError}</div>}
      {saveResult?.issues?.length ? (
        <div className="space-y-2 rounded-2xl border border-amber-200 bg-amber-50 p-4">
          <b className="text-sm">فيه بيانات لسه محتاجة استكمال</b>
          {saveResult.issues.slice(0, 30).map((issue, index) => <div key={`${issue.sheet}-${issue.row}-${index}`} className="text-xs"><b>{labels[issue.sheet] || issue.sheet} — صف {issue.row}:</b> {issue.message}</div>)}
        </div>
      ) : null}

      <div className="rounded-2xl border-2 border-teal-300 bg-teal-50 p-5">
        <div>
          <b className="text-lg">2. راجع البيانات ثم احفظها</b>
          <p className="mt-1 text-xs text-[var(--muted)]">فيه زر حفظ واحد للصفحة كلها. الحفظ هو اللي ينقل الـdraft إلى قاعدة بيانات Linka، وأي صف غير مكتمل يفضل ظاهر كملاحظة بدل ما يوقع باقي الإعداد.</p>
        </div>
        <Button type="button" size="lg" onClick={saveDraft} disabled={saving} className="mt-4 w-full justify-center py-6 text-lg font-bold">{saving ? <><LoaderCircle size={19} className="animate-spin" /> جاري الحفظ</> : <><Save size={19} /> حفظ إعدادات العيادة</>}</Button>

        <div className="mt-5 border-t border-teal-200 pt-5">
          {canContinue ? (
            <Link href="/setup/integration" className={`${buttonVariants({ size: "lg" })} w-full justify-center py-6 text-lg font-bold`}>
              التالي: البيانات التاريخية <ArrowLeft size={18} />
            </Link>
          ) : (
            <div className="space-y-2">
              <Button type="button" size="lg" disabled className="w-full justify-center py-6 text-lg font-bold">التالي: البيانات التاريخية <ArrowLeft size={18} /></Button>
              <p className="text-xs text-amber-800">{dirty ? "فيه تعديلات غير محفوظة. اضغط «حفظ إعدادات العيادة» الأول." : <>كمّل المطلوب الأول: {missing.join(" • ") || "احفظ إعدادات العيادة"}</>}</p>
            </div>
          )}
          <p className="mt-2 text-[11px] text-[var(--muted)]">البيانات التاريخية خطوة اختيارية؛ بعد تجهيز العيادة تقدر تتخطاها وتشغل Linka مباشرة.</p>
        </div>
      </div>
    </div>
  );
}
