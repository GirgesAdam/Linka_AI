import { Banknote, Building2, Landmark, Receipt } from "lucide-react";

import { VaultActionForm } from "@/components/vault-action-form";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatMoney } from "@/lib/format";
import type { MainTreasury, MainTreasuryMovement, ReceptionVault, VaultPendingPayment } from "@/lib/vault";
import { PendingVaultPayments } from "./pending-vault-payments";

import { recordMainBankDeposit } from "../vault-actions";
import { VaultFundingForm } from "../vault-funding-form";

const movementLabels: Record<string, string> = {
  opening: "رصيد افتتاحي سابق",
  manual_funding: "إضافة أموال يدوياً",
  payment: "تحصيل",
  refund: "مرتجع",
  transfer_in: "توريد من الريسبشن",
  transfer_out: "توريد",
  expense: "مصروف",
  correction: "تسوية معتمدة",
};

function Movement({
  entry, branches,
}: {
  entry: MainTreasuryMovement;
  branches: ReceptionVault[];
}) {
  const source = branches.find((b) => b.branch_id === entry.branch_id);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 py-3 text-sm last:border-0">
      <div>
        <div className="font-semibold text-slate-900">{entry.account_kind === "main_bank" && entry.kind === "transfer_in" ? "إيداع في البنك" : entry.account_kind === "main_cash" && entry.kind === "transfer_out" && !entry.branch_id ? "توريد إلى البنك" : movementLabels[entry.kind] || entry.kind}</div>
        <div className="mt-1 text-xs text-slate-500">
          {new Date(entry.created_at).toLocaleString("ar-EG")}
          {" · "}
          {entry.account_kind === "main_cash" ? "Cash" : entry.account_kind === "main_visa" ? "Visa" : entry.account_kind === "main_bank" ? "البنك" : "InstaPay"}
          {source ? ` · من فرع ${source.branch_name}` : null}
        </div>
        {entry.note && <p className="mt-1 text-xs text-slate-600">{entry.note}</p>}
      </div>
      <span className="font-black text-slate-900">{formatMoney(entry.amount_minor, "EGP")}</span>
    </div>
  );
}

export function MainTreasurySection({
  treasury, vaults, movements, pendingPayments, pendingUnavailable, isDemo = false,
}: {
  treasury: MainTreasury;
  vaults: ReceptionVault[];
  movements: MainTreasuryMovement[];
  pendingPayments: VaultPendingPayment[];
  pendingUnavailable: boolean;
  isDemo?: boolean;
}) {
  const channels = [
    { kind: "main_cash" as const, label: "رصيد الكاش", amount: treasury.cash_minor, icon: Banknote, activated: treasury.cash_activated },
    { kind: "main_bank" as const, label: "الرصيد البنكي", amount: treasury.bank_minor, icon: Landmark, activated: treasury.bank_activated },
  ];
  return (
    <Card className="mb-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Building2 size={18} /> الخزنة الرئيسية</CardTitle>
        <p className="mt-1 text-xs text-slate-600">
          الكاش المُورّد فعليًا والرصيد البنكي المسجل (Visa وInstaPay وإيداعات الكاش). الرصيد المسجل لا يثبت التسوية البنكية.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {isDemo && (
          <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
            الخزنة غير متاحة في الوضع التجريبي حالياً.
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          {channels.map(({ kind, label, amount, icon: Icon, activated }) => (
            <div key={kind} className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center gap-2 text-xs font-semibold text-slate-600"><Icon size={15} />{label}</div>
              <div className="mt-2 text-xl font-black text-slate-950">{formatMoney(amount, treasury.currency)}</div>
              {!isDemo && activated && (
                <div className="mt-3 border-t border-slate-100 pt-3">
                  <VaultFundingForm kind={kind} />
                </div>
              )}
              {!isDemo && !activated && (
                <p role="status" className="mt-3 text-xs text-amber-800">الحساب غير متاح حاليًا. أعد تحميل الصفحة.</p>
              )}
            </div>
          ))}
        </div>
        <div className="rounded-xl bg-slate-50 p-4 text-sm">
          إجمالي الرصيد <strong>{formatMoney(treasury.total_recorded_minor, treasury.currency)}</strong>
        </div>
        {!isDemo && treasury.cash_activated && treasury.bank_activated && (
          <section className="rounded-xl border border-slate-200 p-4">
            <h3 className="font-bold">توريد الكاش إلى البنك</h3>
            <VaultActionForm action={recordMainBankDeposit} confirmMessage="هل تؤكد توريد المبلغ إلى البنك؟" submitLabel="تأكيد التوريد" pendingLabel="جاري التسجيل..." className="mt-3 space-y-2">
              <label className="block text-sm">المبلغ بالجنيه<Input name="amount" type="number" min="0.01" step="0.01" required /></label>
            </VaultActionForm>
          </section>
        )}
        {treasury.unattributed_payment_count > 0 && (
          <p className="rounded-xl bg-amber-50 p-3 text-sm text-amber-900" role="status">
            توجد {treasury.unattributed_payment_count} حركة دفع غير مسندة. لم تدخل أرصدة خزنة خاطئة؛ يلزم مراجعتها قبل تسوية الحسابات.
          </p>
        )}
        <PendingVaultPayments payments={pendingPayments} vaults={vaults} unavailable={pendingUnavailable} />
        <section>
          <h3 className="mb-3 flex items-center gap-2 font-bold"><Receipt size={17} /> سجل حركات الخزنة الرئيسية</h3>
          {movements.length ? movements.map((entry) => <Movement key={entry.id} entry={entry} branches={vaults} />) : (
            <p className="rounded-lg border border-dashed p-4 text-sm text-slate-500">لا توجد حركات مسجلة حتى الآن.</p>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
