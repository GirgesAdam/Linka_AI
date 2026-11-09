"use client";

import { PermissionLink as Link } from "@/components/permission-link";
import { useActionState, useState } from "react";
import { CheckCircle2, ListChecks, LoaderCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { createClientRequestId } from "@/lib/utils";
import { createCohortFollowUpAction, type CohortFollowUpState } from "../../actions";

function tomorrowAtTen() {
  const value = new Date();
  value.setDate(value.getDate() + 1);
  value.setHours(10, 0, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

export function SavedCohortFollowUpForm({ cohortId, cohortName, memberCount }: { cohortId: string; cohortName: string; memberCount: number }) {
  const [requestId] = useState(() => createClientRequestId());
  const [dueAt, setDueAt] = useState(() => tomorrowAtTen());
  const [state, action, pending] = useActionState<CohortFollowUpState, FormData>(
    createCohortFollowUpAction,
    { result: null, error: null },
  );

  if (state.result) {
    return <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-950"><div className="flex items-center gap-2 font-black"><CheckCircle2 size={17}/>تم إنشاء {state.result.created_tasks} مهمة جديدة.</div>{state.result.reused_tasks > 0 && <div className="mt-1 text-xs">كان فيه {state.result.reused_tasks} مهمة موجودة بالفعل ولم يتم تكرارها.</div>}<Link href="/tasks" className="mt-2 inline-block text-xs font-bold underline">افتح قائمة المهام</Link></div>;
  }

  return <form action={action} className="grid gap-3 rounded-2xl border border-[var(--border)] bg-white p-4 md:grid-cols-2">
    <input type="hidden" name="cohort_id" value={cohortId}/>
    <input type="hidden" name="request_id" value={requestId}/>
    <label className="text-xs font-bold">عنوان المهمة<input name="title" defaultValue={`متابعة ${cohortName}`} maxLength={200} className="form-control mt-1"/></label>
    <label className="text-xs font-bold">ميعاد الاستحقاق<input type="datetime-local" name="due_at" value={dueAt} onChange={event=>setDueAt(event.target.value)} className="form-control mt-1"/></label>
    <label className="text-xs font-bold">الأولوية<select name="priority" defaultValue="normal" className="form-control mt-1"><option value="low">منخفضة</option><option value="normal">عادية</option><option value="high">عالية</option><option value="urgent">عاجلة</option></select></label>
    <label className="text-xs font-bold">ملاحظة<input name="description" maxLength={5000} placeholder="اختياري" className="form-control mt-1"/></label>
    <div className="md:col-span-2 flex flex-wrap items-center gap-3"><Button type="submit" disabled={pending || !requestId || !dueAt || memberCount===0}>{pending?<LoaderCircle size={16} className="animate-spin"/>:<ListChecks size={16}/>}أنشئ {memberCount} مهام متابعة</Button><span className="text-xs text-[var(--muted)]">مهام للفريق فقط؛ لن يتم إرسال رسائل تلقائيًا.</span></div>
    {state.error&&<div className="md:col-span-2 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{state.error}</div>}
  </form>;
}
