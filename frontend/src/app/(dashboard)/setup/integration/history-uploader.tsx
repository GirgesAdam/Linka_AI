"use client";

import { useActionState, useEffect, useState, useTransition } from "react";
import { CheckCircle2, FileSpreadsheet, LoaderCircle, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { HistoricalBatch } from "@/lib/clinic-setup-v2-types";
import type { HistoricalImportActionState } from "./actions";
import {
  previewHistoricalImportAction,
  readHistoricalImportBatchAction,
  startHistoricalImportAction,
} from "./actions";

const initialHistoricalImportActionState: HistoricalImportActionState = { preview: null, error: null };

const entityLabels: Record<string, string> = {
  patient: "العملاء",
  appointment: "المواعيد",
  payment: "المدفوعات",
  payment_allocation: "توزيع المدفوعات",
  package: "الباقات",
};

function BatchStatus({ batch }: { batch: HistoricalBatch }) {
  if (batch.status === "imported") return <div className="flex items-center gap-2 text-sm font-bold text-emerald-700"><CheckCircle2 size={18} /> تم الاستيراد بنجاح.</div>;
  if (batch.status === "failed") return <div className="rounded-xl bg-red-50 p-3 text-sm font-bold text-red-700"><TriangleAlert size={18} className="mb-1 inline" /> الاستيراد لم يكتمل: {batch.error_message || "حدث خطأ أثناء حفظ البيانات."}</div>;
  if (batch.status === "importing") return <div className="flex items-center gap-2 text-sm font-bold text-teal-700"><LoaderCircle size={18} className="animate-spin" /> جاري استيراد البيانات… يمكنك ترك الصفحة والعودة لاحقًا.</div>;
  return null;
}

export function HistoricalImportUploader({ initialBatch = null }: { initialBatch?: HistoricalBatch | null }) {
  const [batch, setBatch] = useState<HistoricalBatch | null>(initialBatch);
  const [state, formAction, pendingPreview] = useActionState(
    async (
      previous: HistoricalImportActionState,
      formData: FormData,
    ) => {
      const next = await previewHistoricalImportAction(previous, formData);
      if (next.preview?.batch) setBatch(next.preview.batch);
      return next;
    },
    initialHistoricalImportActionState,
  );
  const [pendingApply, startTransition] = useTransition();
  const [applyError, setApplyError] = useState<string | null>(null);
  const batchId = batch?.batch_id ?? null;
  const batchStatus = batch?.status ?? null;

  useEffect(() => {
    if (!batchId || batchStatus !== "importing") return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const latest = await readHistoricalImportBatchAction(batchId);
        if (!cancelled) setBatch(latest);
      } catch {
        // Keep the current state and retry on the next interval. Import state is durable in the backend.
      }
    }, 1600);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [batchId, batchStatus]);

  const preview = state.preview;
  const readyTotal = preview ? Object.values(preview.ready_counts).reduce((sum, value) => sum + value, 0) : 0;
  const rejectedTotal = preview ? Object.values(preview.rejected_counts).reduce((sum, value) => sum + value, 0) : 0;

  return (
    <div className="space-y-5">
      <form action={formAction} className="space-y-4 rounded-2xl border border-[var(--border)] bg-white p-5">
        <div className="flex items-start gap-3"><span className="grid size-10 place-items-center rounded-xl bg-teal-50 text-teal-700"><FileSpreadsheet size={19} /></span><div><b>ارفع بيانات العملاء القديمة</b><p className="mt-1 text-xs leading-5 text-[var(--muted)]">للباقات النشطة استخدم قالب Linka الجديد: Sheet واحدة فقط. ملفات Linka التاريخية القديمة متعددة الشيتات ما زالت مدعومة.</p></div></div>
        <input type="file" name="files" accept=".xlsx,.csv" multiple required className="form-control" />
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="rounded-xl border p-3 text-sm"><input type="radio" name="mode" value="append" defaultChecked className="ml-2" /><b>إضافة للبيانات الحالية</b><div className="mt-1 text-xs text-[var(--muted)]">يحافظ على البيانات الموجودة ويضيف الحقائق الجديدة فقط.</div></label>
          <label className="rounded-xl border p-3 text-sm"><input type="radio" name="mode" value="replace_previous_imports" className="ml-2" /><b>استبدال الاستيرادات السابقة</b><div className="mt-1 text-xs text-[var(--muted)]">يستبدل البيانات التاريخية المستوردة سابقًا فقط.</div></label>
        </div>
        {state.error && <div className="rounded-xl bg-red-50 p-3 text-sm font-bold text-red-700">{state.error}</div>}
        <Button disabled={pendingPreview}>{pendingPreview ? <><LoaderCircle size={16} className="animate-spin" /> جاري الفحص</> : "فحص الملفات"}</Button>
      </form>

      {batch && !preview && (
        <div className="space-y-3 rounded-2xl border border-[var(--border)] bg-white p-5">
          <BatchStatus batch={batch} />
          {(batch.status === "preview_ready" || batch.status === "failed") && (
            <Button disabled={pendingApply} onClick={() => startTransition(async () => {
              setApplyError(null);
              try { setBatch(await startHistoricalImportAction(batch.batch_id)); }
              catch (error) { setApplyError(error instanceof Error ? error.message : "تعذر بدء الاستيراد."); }
            })}>{pendingApply ? <><LoaderCircle size={16} className="animate-spin" /> جاري البدء</> : batch.status === "failed" ? "إعادة محاولة الاستيراد" : "استيراد البيانات"}</Button>
          )}
          {applyError && <div className="rounded-xl bg-red-50 p-3 text-sm font-bold text-red-700">{applyError}</div>}
        </div>
      )}

      {preview && (
        <div className="space-y-4 rounded-2xl border border-[var(--border)] bg-white p-5">
          <div className="flex flex-wrap items-center justify-between gap-3"><div><b className="text-lg">نتيجة الفحص</b><p className="mt-1 text-xs text-[var(--muted)]">Linka تستبعد الصفوف غير الصالحة بدل إسقاط الاستيراد كله.</p></div><div className="flex gap-2"><Badge tone="green">جاهز {readyTotal}</Badge>{rejectedTotal > 0 && <Badge tone="yellow">مستبعد {rejectedTotal}</Badge>}</div></div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            {Object.entries(preview.ready_counts).map(([entity, count]) => <div key={entity} className="rounded-xl bg-slate-50 p-3"><div className="text-xs text-[var(--muted)]">{entityLabels[entity] || entity}</div><b>{count}</b></div>)}
          </div>
          {Object.keys(preview.projected_counts || {}).length > 0 && (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
              <div className="rounded-xl border border-slate-100 p-3"><div className="text-xs text-[var(--muted)]">عملاء جدد</div><b>{preview.projected_counts.patients_to_create ?? 0}</b></div>
              <div className="rounded-xl border border-slate-100 p-3"><div className="text-xs text-[var(--muted)]">عملاء موجودون سيتم ربطهم</div><b>{preview.projected_counts.existing_patients_reused ?? 0}</b></div>
              <div className="rounded-xl border border-slate-100 p-3"><div className="text-xs text-[var(--muted)]">باقات سيتم إنشاؤها</div><b>{preview.projected_counts.packages_to_create ?? 0}</b></div>
              <div className="rounded-xl border border-slate-100 p-3"><div className="text-xs text-[var(--muted)]">مدفوعات تاريخية سيتم إنشاؤها</div><b>{preview.projected_counts.historical_payments_to_create ?? 0}</b></div>
              <div className="rounded-xl border border-slate-100 p-3"><div className="text-xs text-[var(--muted)]">إجمالي المدفوعات المستوردة</div><b>{new Intl.NumberFormat("ar-EG").format((preview.total_amount_imported_minor || 0) / 100)} ج.م</b></div>
            </div>
          )}
          {preview.issue_groups.length > 0 && <div className="space-y-2"><div className="font-black">ملاحظات تحتاج مراجعة</div>{preview.issue_groups.map((issue) => <div key={`${issue.entity_type}-${issue.code}`} className="rounded-xl border border-amber-100 bg-amber-50/60 p-3 text-sm"><div className="flex flex-wrap items-center justify-between gap-2"><b>{issue.message}</b><Badge tone="yellow">{issue.occurrence_count} صف</Badge></div>{issue.example_rows.length > 0 && <div className="mt-1 text-xs text-[var(--muted)]">أمثلة: الصفوف {issue.example_rows.join("، ")}</div>}</div>)}</div>}
          {batch && <BatchStatus batch={batch} />}
          {applyError && <div className="rounded-xl bg-red-50 p-3 text-sm font-bold text-red-700">{applyError}</div>}
          {preview.can_import && batch?.status === "preview_ready" && (
            <Button disabled={pendingApply} onClick={() => startTransition(async () => {
              setApplyError(null);
              try { setBatch(await startHistoricalImportAction(batch.batch_id)); }
              catch (error) { setApplyError(error instanceof Error ? error.message : "تعذر بدء الاستيراد."); }
            })}>{pendingApply ? <><LoaderCircle size={16} className="animate-spin" /> جاري البدء</> : "استيراد البيانات"}</Button>
          )}
        </div>
      )}
    </div>
  );
}
