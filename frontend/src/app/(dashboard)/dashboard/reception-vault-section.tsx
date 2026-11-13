import { Banknote, ArrowLeftRight, Receipt } from "lucide-react";

import { VaultActionForm } from "@/components/vault-action-form";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { formatMoney } from "@/lib/format";
import type { ReceptionVault } from "@/lib/vault";

import { recordReceptionExpense, transferReceptionBalance } from "../vault-actions";

function Amount({ amount, currency }: { amount: number; currency: string }) {
  return <span className="font-black text-slate-950">{formatMoney(amount, currency)}</span>;
}

export function ReceptionVaultSection({
  vaults, unavailable,
}: {
  vaults: ReceptionVault[];
  unavailable: boolean;
}) {
  return (
    <section aria-labelledby="reception-vault-heading" className="mt-8 sm:mt-10">
      <div className="mb-3">
        <h2 id="reception-vault-heading" className="text-xl font-semibold text-slate-950">خزنة الريسبشن</h2>
        <p className="mt-1 text-sm text-slate-500">الكاش الفعلي المتبقي، شامل العهدة المرحلة من الأيام السابقة.</p>
      </div>

      {unavailable ? (
        <div role="alert" className="rounded-xl bg-amber-50 p-4 text-sm text-amber-900">
          تعذر تحميل خزنة الريسبشن. لم يتم تغيير أي رصيد.
        </div>
      ) : vaults.length === 0 ? (
        <div className="rounded-xl bg-white p-5 text-sm text-slate-600">
          لا توجد فروع مصرح لك باستخدام خزنتها. يُرجى مراجعة مدير النظام.
        </div>
      ) : (
        <div className="grid gap-4">
          {vaults.map((vault) => (
            <Card key={vault.branch_id} className="border-slate-100">
              <CardHeader>
                <CardTitle className="flex flex-wrap items-center gap-2">
                  <Banknote size={18} className="text-violet-700" />
                  {vault.branch_name}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-5">
                {!vault.activated ? (
                  <p className="rounded-xl bg-amber-50 p-3 text-sm font-semibold text-amber-900">
                    لم يتم تفعيل خزنة هذا الفرع أو إثبات العهدة الافتتاحية بواسطة المدير.
                  </p>
                ) : (
                  <>
                    <div className="rounded-xl bg-violet-50 p-4">
                      <p className="text-xs font-semibold text-violet-700">الرصيد الحالي</p>
                      <div className="mt-1 text-2xl font-black text-violet-950">
                        {formatMoney(vault.balance_minor, vault.currency)}
                      </div>
                    </div>
                    <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-5">
                      {[
                        ["العهدة المرحلة", vault.opening_carried_minor],
                        ["كاش اليوم", vault.cash_today_minor],
                        ["مرتجعات نقدية", -vault.refunds_today_minor],
                        ["مصروفات اليوم", -vault.expenses_today_minor],
                        ["توريدات اليوم", -vault.transfers_today_minor],
                      ].map(([label, amount]) => (
                        <div key={String(label)} className="rounded-lg border border-slate-100 p-3">
                          <dt className="mb-1 text-xs font-semibold text-slate-500">{label}</dt>
                          <dd><Amount amount={Number(amount)} currency={vault.currency} /></dd>
                        </div>
                      ))}
                    </dl>
                    <div className="grid gap-5 border-t border-slate-100 pt-4 lg:grid-cols-2">
                      <div className="space-y-2">
                        <div className="flex items-center gap-2 font-bold text-slate-900">
                          <ArrowLeftRight size={17} /> توريد العهدة
                        </div>
                        <p className="text-xs text-slate-600">
                          يتم توريد كامل الرصيد المتاح إلى الخزنة الرئيسية دفعة واحدة.
                        </p>
                        <VaultActionForm
                          action={transferReceptionBalance}
                          confirmMessage={`هل تؤكد توريد كامل رصيد ${vault.branch_name} (${formatMoney(vault.balance_minor, vault.currency)}) للخزنة الرئيسية؟`}
                          submitLabel="توريد العهدة"
                          pendingLabel="جاري التوريد..."
                          disabled={vault.balance_minor <= 0}
                          className="space-y-3"
                        >
                          <input type="hidden" name="branch_id" value={vault.branch_id} />
                        </VaultActionForm>
                      </div>
                      <div className="space-y-2">
                        <div className="flex items-center gap-2 font-bold text-slate-900">
                          <Receipt size={17} /> خصم مصروفات
                        </div>
                        <VaultActionForm
                          action={recordReceptionExpense}
                          confirmMessage="هل تؤكد تسجيل هذا المصروف وخصم قيمته من كاش الريسبشن؟"
                          submitLabel="خصم مصروفات"
                          pendingLabel="جاري تسجيل المصروف..."
                          disabled={vault.balance_minor <= 0}
                          className="space-y-3"
                        >
                          <input type="hidden" name="branch_id" value={vault.branch_id} />
                          <label className="block">
                            <span className="mb-1 block text-xs font-semibold text-slate-600">المبلغ بالجنيه</span>
                            <Input name="amount" type="number" min="0.01" step="0.01" required placeholder="0.00" />
                          </label>
                          <label className="block">
                            <span className="mb-1 block text-xs font-semibold text-slate-600">وصف المصروف</span>
                            <Textarea name="description" required rows={2} maxLength={200} placeholder="مثال: مصروفات تشغيل يومية" />
                          </label>
                        </VaultActionForm>
                      </div>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}
