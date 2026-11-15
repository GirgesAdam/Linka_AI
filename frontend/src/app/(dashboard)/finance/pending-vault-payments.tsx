import { AlertTriangle, ShieldCheck } from "lucide-react";

import { ActionFeedbackForm } from "@/components/action-feedback-form";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { formatMoney } from "@/lib/format";
import type { ReceptionVault, VaultPendingPayment } from "@/lib/vault";

import { resolvePendingVaultPayment } from "../vault-actions";
import { ElectronicPendingActions } from "./electronic-pending-actions";

const reasons: Record<string, string> = {
  incomplete_payment_allocations: "التخصيصات غير مكتملة",
  ambiguous_branch: "الدفع موزع على أكثر من فرع أو الفرع غير معروف",
  reception_not_activated: "خزنة الفرع لم تكن مفعلة",
  main_channel_not_activated: "قناة الدفع الرئيسية لم تكن مفعلة",
  unposted_original_payment: "أصل المرتجع غير مُرحّل",
  allocation_patient_mismatch: "تخصيصات الدفعة تخص مريضًا آخر",
};

export function PendingVaultPayments({
  payments, vaults, unavailable,
}: {
  payments: VaultPendingPayment[];
  vaults: ReceptionVault[];
  unavailable: boolean;
}) {
  return (
    <section className="rounded-xl border border-amber-200 p-4" aria-label="المدفوعات المعلقة">
      <h3 className="flex items-center gap-2 text-sm font-black text-slate-900">
        <AlertTriangle size={17} /> مدفوعات تحتاج مراجعة إدارية ({payments.filter((p) => !p.review_outcome).length} لم تُسوّ بعد)
      </h3>
      <p className="mt-2 text-xs text-slate-600">
        لا تُضاف الدفعات غير الموثقة لخزنة أي فرع. الحركات التي تحتوي على أدلة متعارضة أو مدفوعات تسبق التفعيل تتطلب مراجعة مستقلة، وليس اختيارًا عشوائيًا للفرع.
      </p>
      {unavailable ? (
        <p role="alert" className="mt-3 rounded-lg bg-rose-50 p-3 text-xs text-rose-800">تعذر تحميل الحركات المالية المعلقة؛ لا يمكن تأكيد عدم وجودها حاليًا.</p>
      ) : payments.length === 0 ? (
        <p className="mt-3 text-xs text-slate-500">لا توجد حركات معلقة.</p>
      ) : (
        <div className="mt-4 space-y-4">
          {payments.map((payment) => (
            <article key={payment.pending_id} className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div className="text-sm font-bold">{formatMoney(payment.amount_minor, payment.currency)}</div>
                  <p className="mt-1 text-xs text-slate-500">
                    {payment.payment_method.toUpperCase()} · {payment.transaction_type} · {payment.source} · {new Date(payment.created_at).toLocaleString("ar-EG")}
                  </p>
                  <p className="mt-1 break-all text-[11px] text-slate-500">مرجع الدفعة: {payment.transaction_id}</p>
                  {payment.external_reference && (
                    <p className="mt-1 break-words text-xs text-slate-500">مرجع خارجي: {payment.external_reference}</p>
                  )}
                </div>
                <span className="rounded-lg bg-amber-50 px-2 py-1 text-xs font-bold text-amber-900">
                  {reasons[payment.reason] || payment.reason}
                </span>
              </div>
              <div className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-700">
                <p className="font-bold">دليل إسناد الفرع من الدفعة الأصلية</p>
                {payment.allocations.length === 0 ? (
                  <p className="mt-1">لا توجد Allocations موثقة لهذه الدفعة.</p>
                ) : (
                  payment.allocations.map((allocation) => (
                    <p className="mt-1" key={allocation.allocation_id}>
                      {allocation.branch_name || "فرع غير محدد"} · {formatMoney(allocation.amount_minor, payment.currency)}
                      {" · "}Appointment: {allocation.appointment_id}
                      {!allocation.patient_matches_source ? " · تحذير: المريض غير مطابق للمصدر" : null}
                    </p>
                  ))
                )}
                {payment.appointment_id && (
                  <p className="mt-2 break-all text-[11px]">مرجع Appointment المصدر: {payment.appointment_id}</p>
                )}
              </div>
              {payment.payment_method === "cash" ? (payment.resolution_eligible && payment.verified_branch_id ? (
                <ActionFeedbackForm
                  action={resolvePendingVaultPayment}
                  className="mt-4 space-y-3"
                  submitLabel="اعتماد وترحيل الدفعة"
                  pendingLabel="جاري الترحيل..."
                  refreshAfterSuccess
                >
                  <input type="hidden" name="pending_id" value={payment.pending_id} />
                  <label className="block">
                    <span className="mb-1 block text-xs font-semibold text-slate-600">الفرع المثبت بالمستندات</span>
                    <Select name="branch_id" required defaultValue={payment.verified_branch_id}>
                      {vaults.filter((vault) => vault.activated).map((vault) => (
                        <option key={vault.branch_id} value={vault.branch_id}>{vault.branch_name}</option>
                      ))}
                    </Select>
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-semibold text-slate-600">
                      سبب اعتماد التسوية والمرجع الداعم
                    </span>
                    <Textarea name="explanation" required minLength={8} maxLength={500} rows={2}
                      placeholder="مثال: تمت مراجعة سجلات التخصيص وإثبات ارتباط المبلغ بالكامل بالفرع..." />
                  </label>
                  <p className="flex items-center gap-1 text-xs text-emerald-800">
                    <ShieldCheck size={14} /> الفرع والمبلغ سيتحقق منهما السيرفر مجددًا.
                  </p>
                </ActionFeedbackForm>
              ) : (
                <p className="mt-3 text-xs font-semibold text-amber-900">
                  لا يمكن الترحيل من هذه الشاشة حاليًا: يلزم اكتمال دليل الإسناد أو معالجة سبب التعليق دون تخمين.
                </p>
              )) : (<ElectronicPendingActions payment={payment} />)}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
