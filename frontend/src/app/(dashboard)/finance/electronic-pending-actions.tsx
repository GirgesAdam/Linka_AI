import { VaultActionForm } from "@/components/vault-action-form";
import type { VaultPendingPayment } from "@/lib/vault";

import {
  retryEligibleElectronicReceipt,
  reviewElectronicOpeningReceipt,
} from "../vault-actions";

export function ElectronicPendingActions({ payment }: { payment: VaultPendingPayment }) {
  if (payment.review_outcome) {
    return (
      <div className="mt-3 rounded-lg bg-emerald-50 p-3 text-xs text-emerald-900" role="status">
        <strong>مراجعة إدارية مسجلة</strong>
        <p className="mt-1">
          {payment.review_outcome === "included_in_opening"
            ? "الدفعة مشمولة في الرصيد الافتتاحي وفق اعتماد المدير، ولم تُرحّل كإيراد نقدي جديد."
            : "الدفعة معلقة لحين اتخاذ قرار محاسبي؛ لم يجرِ ترحيلها."}
        </p>
        {payment.reviewed_at ? <p className="mt-1">تاريخ المراجعة: {new Date(payment.reviewed_at).toLocaleString("ar-EG")}</p> : null}
        {payment.review_note ? <p className="mt-1 break-words">الدليل المسجل: {payment.review_note}</p> : null}
      </div>
    );
  }
  if (payment.electronic_action === "opening_review") {
    return (
      <VaultActionForm
        action={reviewElectronicOpeningReceipt}
        className="mt-4 space-y-2"
        confirmMessage="هل تؤكد مراجعة الدفعة الإلكترونية وتحديد علاقتها بالرصيد الافتتاحي؟ لا يتم إضافة أي رصيد تلقائيًا."
        submitLabel="تسجيل المراجعة المالية"
        pendingLabel="جاري حفظ قرار المراجعة..."
      >
        <input type="hidden" name="pending_id" value={payment.pending_id} />
        <label className="block text-xs font-semibold">
          نتيجة مراجعة الدفعة السابقة لتفعيل القناة
          <select name="outcome" required defaultValue="" className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2">
            <option value="" disabled>اختر النتيجة بعد المطابقة مع الرصيد الافتتاحي</option>
            <option value="included_in_opening">الدفعة مشمولة ضمن الرصيد الافتتاحي المعلن</option>
            <option value="requires_accounting_decision">غير مثبتة ضمن الرصيد؛ تستلزم قرارًا محاسبيًا</option>
          </select>
        </label>
        <label className="block text-xs font-semibold">
          مستند المراجعة أو سبب القرار
          <textarea name="explanation" required minLength={8} maxLength={500} rows={2}
            className="mt-1 w-full rounded-lg border border-slate-300 p-2"
            placeholder="بيان الإيداع، المطابقة البنكية، أو سبب تعليق التسوية" />
        </label>
        <p className="text-xs text-amber-800">
          لا يتم ترحيل الحركات السابقة للتفعيل مرة ثانية. إثبات شمولها في الرصيد الافتتاحي يخضع للتحقق من سعة الرصيد على السيرفر.
        </p>
      </VaultActionForm>
    );
  }
  if (payment.electronic_action === "eligible_retry") {
    return (
      <VaultActionForm
        action={retryEligibleElectronicReceipt}
        className="mt-4 space-y-2"
        confirmMessage="إعادة محاولة ترحيل هذه الدفعة بعد التأكد أنها سُجلت بعد تفعيل القناة؟"
        submitLabel="اعتماد إعادة الترحيل"
        pendingLabel="جاري التحقق من المصدر والترحيل..."
      >
        <input type="hidden" name="pending_id" value={payment.pending_id} />
        <label className="block text-xs font-semibold">
          سبب إعادة الترحيل
          <textarea name="explanation" required minLength={8} maxLength={500} rows={2}
            className="mt-1 w-full rounded-lg border border-slate-300 p-2"
            placeholder="توثيق أن تاريخ الدفع بعد تفعيل القناة ولم يُرحّل سابقًا" />
        </label>
      </VaultActionForm>
    );
  }
  return (
    <p className="mt-3 text-xs font-semibold text-amber-900">
      لم تُفعّل قناة الدفع المطلوبة بعد أو لا توجد أدلة كافية للتسوية. فعّل القناة أولًا مع رصيد افتتاحي مُطابق، ثم راجع الدفعة هنا. لن يُضاف مبلغ قديم تلقائيًا.
    </p>
  );
}
