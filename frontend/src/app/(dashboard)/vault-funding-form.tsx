import { VaultActionForm } from "@/components/vault-action-form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

import { recordVaultFunding } from "./vault-actions";

export function VaultFundingForm({
  kind, branchId,
}: {
  kind: "reception" | "main_cash" | "main_bank";
  branchId?: string;
}) {
  return (
    <VaultActionForm
      action={recordVaultFunding}
      confirmMessage="هل تؤكد إضافة الأموال الفعلية إلى هذا الحساب؟ لا تُسجل كإيراد ولا يمكن حذف الحركة."
      submitLabel="إضافة أموال"
      pendingLabel="جاري إضافة الأموال..."
      className="space-y-2"
    >
      <input type="hidden" name="kind" value={kind} />
      {branchId && <input type="hidden" name="branch_id" value={branchId} />}
      <label className="block text-xs font-semibold text-slate-600">
        المبلغ بالجنيه
        <Input name="amount" type="number" min="0.01" max="10000000000" step="0.01" required placeholder="0.00" />
      </label>
      <label className="block text-xs font-semibold text-slate-600">
        ملاحظات (اختياري)
        <Textarea name="note" rows={2} maxLength={500} placeholder="سبب إضافة الأموال" />
      </label>
    </VaultActionForm>
  );
}
