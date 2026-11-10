"use client";

import { PermissionLink as Link } from "@/components/permission-link";
import { useActionState, useState } from "react";
import { CheckCircle2, LoaderCircle, ListChecks, UsersRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import { createClientRequestId } from "@/lib/utils";
import type { AnalyticsBIAnswer } from "@/lib/types";
import {
  createAnalyticsCohortAction,
  createCohortFollowUpAction,
  type AnalyticsCohortState,
  type CohortFollowUpState,
} from "./actions";

const cohortableOperations = new Set([
  "top_repeat_patients",
  "top_value_patients",
  "lapsed_patients",
]);

const cohortNames: Record<string, string> = {
  top_repeat_patients: "العملاء الأكثر تكرارًا",
  top_value_patients: "العملاء الأعلى قيمة",
  lapsed_patients: "العملاء المنقطعين",
};

function localTomorrowAtTen() {
  const value = new Date();
  value.setDate(value.getDate() + 1);
  value.setHours(10, 0, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

export function AnalyticsCohortActions({ result }: { result: AnalyticsBIAnswer }) {
  const [cohortRequestId] = useState(() => createClientRequestId());
  const [taskRequestId] = useState(() => createClientRequestId());
  const [defaultDue, setDefaultDue] = useState(() => localTomorrowAtTen());
  const [cohortState, cohortAction, cohortPending] = useActionState<AnalyticsCohortState, FormData>(
    createAnalyticsCohortAction,
    { cohort: null, error: null },
  );
  const [taskState, taskAction, taskPending] = useActionState<CohortFollowUpState, FormData>(
    createCohortFollowUpAction,
    { result: null, error: null },
  );
  const defaultName = cohortNames[result.plan.operation] || "قائمة عملاء";

  if (!cohortableOperations.has(result.plan.operation) || result.rows.length === 0) return null;

  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50/60 p-4">
      <div className="flex items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-indigo-700 text-white"><UsersRound size={18} /></span>
        <div>
          <div className="font-black text-indigo-950">احفظ العملاء كقائمة</div>
          <p className="mt-1 text-xs leading-5 text-indigo-900/70">احفظ العملاء الظاهرين حاليًا كقائمة ثابتة تقدر ترجع لها وتعمل عليها متابعة بعدين.</p>
        </div>
      </div>

      {!cohortState.cohort ? (
        <form action={cohortAction} className="mt-4 flex flex-col gap-3 sm:flex-row">
          <input type="hidden" name="request_id" value={cohortRequestId} />
          <input type="hidden" name="question" value={result.question} />
          <input type="hidden" name="plan" value={JSON.stringify(result.plan)} />
          <input name="name" defaultValue={defaultName} maxLength={160} className="flex-1 rounded-xl border border-indigo-200 bg-white px-3 py-2 text-sm" />
          <Button type="submit" disabled={cohortPending || !cohortRequestId}>
            {cohortPending ? <LoaderCircle size={16} className="animate-spin" /> : <UsersRound size={16} />}
            حفظ القائمة
          </Button>
        </form>
      ) : (
        <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-950">
          <div className="flex items-center gap-2 font-black"><CheckCircle2 size={17} />تم حفظ {cohortState.cohort.member_count} عميل في “{cohortState.cohort.name}”.</div>
          <p className="mt-1 text-xs">القائمة تحتفظ بالعملاء الموجودين فيها وقت الحفظ، ومش بتتغير تلقائيًا بعد كده. <Link href={`/analytics/cohorts/${cohortState.cohort.id}`} className="font-bold underline">فتح القائمة</Link></p>
        </div>
      )}
      {cohortState.error && <div className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{cohortState.error}</div>}

      {cohortState.cohort && !taskState.result && (
        <form action={taskAction} className="mt-4 grid gap-3 rounded-xl border border-indigo-200 bg-white p-4 md:grid-cols-2">
          <input type="hidden" name="cohort_id" value={cohortState.cohort.id} />
          <input type="hidden" name="request_id" value={taskRequestId} />
          <label className="text-xs font-bold text-slate-700">عنوان المهمة<input name="title" defaultValue={`متابعة ${cohortState.cohort.name}`} maxLength={200} className="form-control mt-1" /></label>
          <label className="text-xs font-bold text-slate-700">ميعاد الاستحقاق<input type="datetime-local" name="due_at" value={defaultDue} onChange={event => setDefaultDue(event.target.value)} className="form-control mt-1" /></label>
          <label className="text-xs font-bold text-slate-700">الأولوية<select name="priority" defaultValue="normal" className="form-control mt-1"><option value="low">منخفضة</option><option value="normal">عادية</option><option value="high">مرتفعة</option><option value="urgent">عاجلة</option></select></label>
          <label className="text-xs font-bold text-slate-700">ملاحظة<input name="description" placeholder="اختياري" maxLength={5000} className="form-control mt-1" /></label>
          <div className="md:col-span-2 flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={taskPending || cohortState.cohort.member_count === 0 || !taskRequestId || !defaultDue}>
              {taskPending ? <LoaderCircle size={16} className="animate-spin" /> : <ListChecks size={16} />}
              أنشئ {cohortState.cohort.member_count} مهام متابعة
            </Button>
            <span className="text-xs text-slate-500">الخطوة دي تنشئ مهام متابعة للفريق فقط، من غير إرسال رسائل تلقائيًا.</span>
          </div>
        </form>
      )}

      {taskState.result && (
        <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-950">
          <div className="font-black">تم إنشاء {taskState.result.created_tasks} مهمة جديدة، و{taskState.result.reused_tasks} مهمة كانت موجودة بالفعل.</div>
          <Link href="/tasks" className="mt-2 inline-block text-xs font-bold text-emerald-800 underline">افتح قائمة المهام</Link>
        </div>
      )}
      {taskState.error && <div className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{taskState.error}</div>}
    </div>
  );
}
