"use client";

import Link from "next/link";
import { useActionState, useMemo, useState } from "react";
import { CheckCircle2, LoaderCircle, Megaphone, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { createClientRequestId } from "@/lib/utils";
import type { ChannelConnection } from "@/lib/types";
import {
  confirmCohortCampaignAction,
  prepareCohortCampaignAction,
  type CohortCampaignState,
} from "../../actions";

const initialState: CohortCampaignState = { campaign: null, result: null, error: null };

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    eligible: "جاهز للإرسال",
    skipped_no_consent: "لن يُرسل: لم يوافق على الرسائل التسويقية",
    skipped_inactive: "لن يُرسل: العميل غير نشط",
    skipped_no_route: "لن يُرسل: واتساب غير متاح",
    cancelled_no_consent: "أُلغي: تم سحب الموافقة",
    cancelled_inactive: "أُلغي: العميل غير نشط",
    cancelled_no_route: "أُلغي: واتساب غير متاح",
    queued: "بانتظار الإرسال",
    processing: "جاري الإرسال",
    sent: "تم الإرسال",
    delivered: "تم الوصول",
    read: "تمت القراءة",
    failed: "فشل الإرسال",
    cancelled: "ملغى",
  };
  return labels[status] || "غير متاح";
}

export function SavedCohortCampaignForm({
  cohortId,
  cohortName,
  memberCount,
  whatsappConnections,
  isAdmin,
}: {
  cohortId: string;
  cohortName: string;
  memberCount: number;
  whatsappConnections: ChannelConnection[];
  isAdmin: boolean;
}) {
  const [requestId] = useState(() => createClientRequestId());
  const [confirmationId] = useState(() => createClientRequestId());
  const [prepareState, prepareAction, preparePending] = useActionState<CohortCampaignState, FormData>(prepareCohortCampaignAction, initialState);
  const [confirmState, confirmAction, confirmPending] = useActionState<CohortCampaignState, FormData>(confirmCohortCampaignAction, initialState);

  const campaign = prepareState.campaign;
  const result = confirmState.result;
  const skippedCount = useMemo(() => campaign?.recipients.filter((row) => row.status !== "eligible").length || 0, [campaign]);

  if (!isAdmin) {
    return <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-950">إرسال الحملات متاح لمدير العيادة فقط. ما زال بإمكانك مراجعة القائمة وإنشاء مهام متابعة للفريق.</div>;
  }

  if (!whatsappConnections.length) {
    return <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-950">واتساب غير متصل حاليًا. <Link href="/channels" className="font-bold underline">افتح قنوات التواصل</Link> لإكمال الربط أولًا.</div>;
  }

  if (result) {
    return <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-950">
      <div className="flex items-center gap-2 font-black"><CheckCircle2 size={17}/>تم تأكيد الحملة وتجهيز {result.queued_count.toLocaleString("ar-EG")} رسالة للإرسال.</div>
      {result.cancelled_before_queue > 0 && <div className="mt-1 text-xs">تم استبعاد {result.cancelled_before_queue.toLocaleString("ar-EG")} عميل بعد المراجعة النهائية.</div>}
    </div>;
  }

  if (campaign) {
    return <div className="space-y-4 rounded-2xl border border-indigo-200 bg-indigo-50/50 p-4">
      <div>
        <div className="flex items-center gap-2 font-black text-indigo-950"><ShieldCheck size={18}/>المراجعة جاهزة — لم يتم إرسال أي رسالة بعد</div>
        <div className="mt-1 text-xs leading-5 text-indigo-900">سيتم الإرسال إلى {campaign.eligible_count.toLocaleString("ar-EG")} من أصل {campaign.recipient_count.toLocaleString("ar-EG")} عميل، واستبعاد {skippedCount.toLocaleString("ar-EG")} حسب حالة كل عميل وموافقته.</div>
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        {campaign.recipients.map((row) => <div key={row.id} className="rounded-xl border border-indigo-100 bg-white p-3 text-xs">
          <div className="font-bold">{row.patient_name}</div>
          <div className="mt-1 text-[var(--muted)]">{statusLabel(row.status)}</div>
        </div>)}
      </div>
      <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-6 text-amber-950">قبل الإرسال، Linka هتراجع حالة العملاء وموافقتهم على الرسائل مرة أخيرة. أي عميل لم يعد مؤهلًا سيتم استبعاده تلقائيًا.</div>
      <form action={confirmAction}>
        <input type="hidden" name="campaign_id" value={campaign.id}/>
        <input type="hidden" name="confirmation_id" value={confirmationId}/>
        <Button type="submit" disabled={confirmPending || !confirmationId || campaign.eligible_count === 0}>
          {confirmPending ? <LoaderCircle size={16} className="animate-spin"/> : <Megaphone size={16}/>} تأكيد وإرسال لـ{campaign.eligible_count.toLocaleString("ar-EG")} عميل
        </Button>
      </form>
      {(prepareState.error || confirmState.error) && <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{prepareState.error || confirmState.error}</div>}
    </div>;
  }

  return <form action={prepareAction} className="grid gap-3 rounded-2xl border border-[var(--border)] bg-white p-4 md:grid-cols-2">
    <input type="hidden" name="cohort_id" value={cohortId}/>
    <input type="hidden" name="request_id" value={requestId}/>
    <label className="text-xs font-bold">اسم الحملة<input name="name" defaultValue={`واتساب · ${cohortName}`} maxLength={160} className="form-control mt-1"/></label>
    <label className="text-xs font-bold">حساب واتساب<select name="channel_connection_id" required className="form-control mt-1">{whatsappConnections.map((connection) => <option key={connection.id} value={connection.id}>{connection.display_name}</option>)}</select></label>
    <label className="text-xs font-bold md:col-span-2">قالب الرسالة المعتمد<input name="template_name" required placeholder="اسم القالب المعتمد في WhatsApp" dir="ltr" className="form-control mt-1"/><span className="mt-1 block font-normal text-[var(--muted)]">استخدم اسم قالب رسالة تمت الموافقة عليه مسبقًا في حساب واتساب.</span></label>

    <details className="md:col-span-2 rounded-xl border border-[var(--border)] bg-slate-50 p-3">
      <summary className="cursor-pointer text-xs font-black">خيارات متقدمة</summary>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <label className="text-xs font-bold">لغة القالب<input name="template_language" defaultValue="ar" required dir="ltr" className="form-control mt-1"/></label>
        <label className="text-xs font-bold">سرعة الإرسال<select name="rate_limit_per_minute" defaultValue="10" className="form-control mt-1"><option value="5">هادئة</option><option value="10">عادية</option><option value="20">سريعة</option><option value="30">أسرع</option></select></label>
        <div className="text-xs font-bold md:col-span-2">بيانات يمكن إدراجها في القالب<div className="mt-2 flex flex-wrap gap-3 font-normal"><label><input type="checkbox" name="body_parameter_keys" value="patient_first_name" className="me-1"/>اسم العميل الأول</label><label><input type="checkbox" name="body_parameter_keys" value="clinic_name" className="me-1"/>اسم العيادة</label><label><input type="checkbox" name="body_parameter_keys" value="cohort_name" className="me-1"/>اسم المجموعة</label></div></div>
      </div>
    </details>

    <div className="md:col-span-2 rounded-xl bg-slate-50 p-3 text-xs leading-6 text-[var(--muted)]">الخطوة التالية مراجعة فقط ولن ترسل رسائل. عدد العملاء في القائمة حاليًا {memberCount.toLocaleString("ar-EG")}.</div>
    <div className="md:col-span-2"><Button type="submit" disabled={preparePending || !requestId}>{preparePending ? <LoaderCircle size={16} className="animate-spin"/> : <ShieldCheck size={16}/>} مراجعة المستلمين قبل الإرسال</Button></div>
    {prepareState.error && <div className="md:col-span-2 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{prepareState.error}</div>}
  </form>;
}
