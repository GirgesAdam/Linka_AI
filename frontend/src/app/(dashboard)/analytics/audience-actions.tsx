"use client";

import Link from "next/link";
import { useActionState, useMemo, useState } from "react";
import { CheckCircle2, ListChecks, LoaderCircle, MessageSquareMore, Save, UsersRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import { createClientRequestId } from "@/lib/utils";
import type { AnalyticsAudiencePlan, AnalyticsBIResultRow, AnalyticsCatalogAction } from "@/lib/types";
import {
  confirmAnalyticsAudienceAction,
  type AnalyticsAudienceActionState,
} from "./actions";

function localDue(days: number) {
  const value = new Date();
  value.setDate(value.getDate() + Math.max(0, days));
  value.setHours(10, 0, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

type AudienceActionSource = {
  question:string;
  mode:"audience";
  audience_plan:AnalyticsAudiencePlan|null;
  rows:AnalyticsBIResultRow[];
};

type ActionKind = "save_audience" | "follow_up_tasks" | "whatsapp_campaign";

const actionCopy: Record<ActionKind, { title:string; description:string; submit:string }> = {
  save_audience: {
    title: "حفظ المجموعة",
    description: "احتفظ بالقائمة علشان ترجع لها أو تستخدمها بعدين.",
    submit: "حفظ المجموعة",
  },
  follow_up_tasks: {
    title: "مهام متابعة",
    description: "اعمل مهمة متابعة لكل عميل في القائمة.",
    submit: "إنشاء مهام المتابعة",
  },
  whatsapp_campaign: {
    title: "حملة WhatsApp",
    description: "احفظ القائمة ثم راجع المستلمين والرسالة قبل الإرسال.",
    submit: "حفظ وفتح تجهيز WhatsApp",
  },
};

export function AnalyticsAudienceActions({ result, allowedActions }: { result: AudienceActionSource; allowedActions?: AnalyticsCatalogAction[] }) {
  const plan = result.audience_plan;
  const enabledKinds = useMemo<ActionKind[]>(() => {
    const allowed = new Set(allowedActions || ["save_patient_group", "follow_up_tasks", "whatsapp_campaign"]);
    const items: ActionKind[] = [];
    if (allowed.has("save_patient_group")) items.push("save_audience");
    if (allowed.has("follow_up_tasks")) items.push("follow_up_tasks");
    if (allowed.has("whatsapp_campaign")) items.push("whatsapp_campaign");
    return items;
  }, [allowedActions]);
  const [audienceRequestId] = useState(() => createClientRequestId());
  const [actionRequestId] = useState(() => createClientRequestId());
  const [dueAt, setDueAt] = useState(() => localDue(1));
  const [actionKind, setActionKind] = useState<ActionKind>(
    () => enabledKinds[0] ?? "save_audience",
  );
  const [state, action, pending] = useActionState<AnalyticsAudienceActionState, FormData>(
    confirmAnalyticsAudienceAction,
    { result: null, error: null },
  );
  const defaultName = useMemo(() => {
    const compact = result.question.replace(/\s+/g, " ").trim();
    return compact.length <= 70 ? compact : `${compact.slice(0, 67)}...`;
  }, [result.question]);

  if (result.mode !== "audience" || !plan || result.rows.length === 0 || enabledKinds.length === 0) return null;

  const completed = state.result;
  const copy = actionCopy[actionKind];

  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50/60 p-4">
      <div className="flex items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-indigo-700 text-white"><UsersRound size={18} /></span>
        <div>
          <div className="font-black text-indigo-950">استخدم قائمة العملاء دي</div>
          <p className="mt-1 text-xs leading-5 text-indigo-900/70">
            اختر الخطوة التالية. قبل التنفيذ، Linka هتراجع نفس الشروط مرة أخرى وتتأكد إن القائمة ما زالت صحيحة.
          </p>
        </div>
      </div>

      {!completed && (
        <form action={action} className="mt-4 rounded-xl border border-indigo-200 bg-white p-4">
          <input type="hidden" name="request_id" value={actionRequestId} />
          <input type="hidden" name="audience_request_id" value={audienceRequestId} />
          <input type="hidden" name="question" value={result.question} />
          <input type="hidden" name="plan" value={JSON.stringify(plan)} />
          <input type="hidden" name="action_kind" value={actionKind} />

          <div className={`grid gap-2 ${enabledKinds.length >= 3 ? "md:grid-cols-3" : enabledKinds.length === 2 ? "md:grid-cols-2" : ""}`}>
            {enabledKinds.includes("save_audience") && <button type="button" onClick={() => setActionKind("save_audience")} className={`rounded-xl border p-3 text-right ${actionKind === "save_audience" ? "border-indigo-500 bg-indigo-50" : "border-[var(--border)]"}`}>
              <div className="flex items-center gap-2 font-black"><Save size={16} />{actionCopy.save_audience.title}</div>
              <div className="mt-1 text-[11px] leading-5 text-[var(--muted)]">{actionCopy.save_audience.description}</div>
            </button>}
            {enabledKinds.includes("follow_up_tasks") && <button type="button" onClick={() => setActionKind("follow_up_tasks")} className={`rounded-xl border p-3 text-right ${actionKind === "follow_up_tasks" ? "border-indigo-500 bg-indigo-50" : "border-[var(--border)]"}`}>
              <div className="flex items-center gap-2 font-black"><ListChecks size={16} />{actionCopy.follow_up_tasks.title}</div>
              <div className="mt-1 text-[11px] leading-5 text-[var(--muted)]">{actionCopy.follow_up_tasks.description}</div>
            </button>}
            {enabledKinds.includes("whatsapp_campaign") && <button type="button" onClick={() => setActionKind("whatsapp_campaign")} className={`rounded-xl border p-3 text-right ${actionKind === "whatsapp_campaign" ? "border-indigo-500 bg-indigo-50" : "border-[var(--border)]"}`}>
              <div className="flex items-center gap-2 font-black"><MessageSquareMore size={16} />{actionCopy.whatsapp_campaign.title}</div>
              <div className="mt-1 text-[11px] leading-5 text-[var(--muted)]">{actionCopy.whatsapp_campaign.description}</div>
            </button>}
          </div>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <label className="text-xs font-bold text-slate-700 md:col-span-2">
              اسم المجموعة
              <input name="name" defaultValue={defaultName} maxLength={160} className="form-control mt-1" />
            </label>

            {actionKind === "follow_up_tasks" && (
              <>
                <label className="text-xs font-bold text-slate-700">
                  عنوان المتابعة
                  <input name="title" defaultValue={`متابعة ${defaultName}`} maxLength={200} className="form-control mt-1" />
                </label>
                <label className="text-xs font-bold text-slate-700">
                  ميعاد المتابعة
                  <input type="datetime-local" name="due_at" value={dueAt} onChange={event => setDueAt(event.target.value)} className="form-control mt-1" />
                </label>
                <label className="text-xs font-bold text-slate-700">
                  الأولوية
                  <select name="priority" defaultValue="normal" className="form-control mt-1">
                    <option value="low">منخفضة</option><option value="normal">عادية</option><option value="high">عالية</option><option value="urgent">عاجلة</option>
                  </select>
                </label>
                <label className="text-xs font-bold text-slate-700">
                  ملاحظة
                  <input name="description" defaultValue="" maxLength={5000} placeholder="اختياري" className="form-control mt-1" />
                </label>
              </>
            )}
          </div>

          {actionKind === "whatsapp_campaign" && (
            <p className="mt-4 rounded-xl bg-slate-50 p-3 text-[11px] leading-5 text-slate-600">
              الخطوة دي لا ترسل أي رسالة. هتفتح صفحة مراجعة الحملة لاختيار حساب واتساب والقالب المعتمد ومراجعة المستلمين قبل التأكيد.
            </p>
          )}

          <Button type="submit" className="mt-4 w-full" disabled={pending || !audienceRequestId || !actionRequestId || (actionKind === "follow_up_tasks" && !dueAt)}>
            {pending ? <LoaderCircle size={16} className="animate-spin" /> : actionKind === "save_audience" ? <Save size={16} /> : actionKind === "follow_up_tasks" ? <ListChecks size={16} /> : <MessageSquareMore size={16} />}
            {copy.submit}
          </Button>
        </form>
      )}

      {completed && (
        <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-950">
          <div className="flex items-center gap-2 font-black"><CheckCircle2 size={17} />تم التنفيذ على {completed.audience.member_count} عميل.</div>
          {completed.next_step === "tasks_created" && completed.follow_up && (
            <p className="mt-1 text-xs">اتعمل {completed.follow_up.created_tasks} مهمة جديدة، و{completed.follow_up.reused_tasks} كانت موجودة من محاولة سابقة. <Link href="/tasks" className="font-bold underline">افتح المهام</Link></p>
          )}
          {completed.next_step === "saved" && (
            <p className="mt-1 text-xs">تم حفظ المجموعة. <Link href={`/analytics/cohorts/${completed.audience.id}`} className="font-bold underline">افتح المجموعة</Link></p>
          )}
          {completed.next_step === "campaign_setup" && (
            <p className="mt-1 text-xs">تم حفظ المجموعة فقط، ومفيش رسالة اتبعتت. <Link href={`/analytics/cohorts/${completed.audience.id}`} className="font-bold underline">افتح تجهيز WhatsApp</Link></p>
          )}
        </div>
      )}
      {state.error && <div className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{state.error}</div>}
    </div>
  );
}
