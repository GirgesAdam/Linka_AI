import { Banknote, Building2, CreditCard, Receipt, ShieldCheck, WalletCards } from "lucide-react";

import { ActionFeedbackForm } from "@/components/action-feedback-form";
import { VaultActionForm } from "@/components/vault-action-form";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { formatMoney } from "@/lib/format";
import type { WorkspaceMember } from "@/lib/types";
import type { MainTreasury, MainTreasuryMovement, ReceptionVault, VaultPendingPayment } from "@/lib/vault";
import { PendingVaultPayments } from "./pending-vault-payments";

import { activateVaultAccount, setReceptionBranchGrant } from "../vault-actions";

type Grant = { user_id: string; branch_id: string };
type TreasuryFilters = {
  treasury_kind?: string;
  treasury_method?: string;
  treasury_branch?: string;
  treasury_from?: string;
  treasury_to?: string;
};

const movementLabels: Record<string, string> = {
  opening: "رصيد افتتاحي",
  payment: "تحصيل",
  refund: "مرتجع",
  transfer_in: "توريد من الريسبشن",
  transfer_out: "توريد",
  expense: "مصروف",
  correction: "تسوية معتمدة",
};

function ActivationForm({
  kind, branch,
}: {
  kind: "reception" | "main_cash" | "main_visa" | "main_instapay";
  branch?: ReceptionVault;
}) {
  return (
    <VaultActionForm
      action={activateVaultAccount}
      confirmMessage={`هل تؤكد الرصيد الافتتاحي الفعلي لحساب ${branch ? branch.branch_name : kind}؟ لا يمكن تعديل هذا السجل بعد التفعيل.`}
      submitLabel="تفعيل الحساب"
      pendingLabel="جاري التفعيل..."
      className="space-y-2"
    >
      <input type="hidden" name="kind" value={kind} />
      {branch ? <input type="hidden" name="branch_id" value={branch.branch_id} /> : null}
      <label className="block">
        <span className="mb-1 block text-xs font-semibold text-slate-600">الرصيد الافتتاحي بالجنيه (صفر مسموح)</span>
        <Input name="opening_balance" type="number" min="0" step="0.01" required defaultValue="0" />
      </label>
    </VaultActionForm>
  );
}

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
        <div className="font-semibold text-slate-900">{movementLabels[entry.kind] || entry.kind}</div>
        <div className="mt-1 text-xs text-slate-500">
          {new Date(entry.created_at).toLocaleString("ar-EG")}
          {" · "}
          {entry.account_kind === "main_cash" ? "Cash" : entry.account_kind === "main_visa" ? "Visa" : "InstaPay"}
          {source ? ` · من فرع ${source.branch_name}` : null}
        </div>
      </div>
      <span className="font-black text-slate-900">{formatMoney(entry.amount_minor, "EGP")}</span>
    </div>
  );
}

export function MainTreasurySection({
  treasury, vaults, members, grants, movements, pendingPayments, pendingUnavailable, filters, isDemo = false,
}: {
  treasury: MainTreasury;
  vaults: ReceptionVault[];
  members: WorkspaceMember[];
  grants: Grant[];
  movements: MainTreasuryMovement[];
  pendingPayments: VaultPendingPayment[];
  pendingUnavailable: boolean;
  filters: TreasuryFilters;
  isDemo?: boolean;
}) {
  const channels = [
    { kind: "main_cash" as const, label: "كاش مستلم من الريسبشن", amount: treasury.cash_minor, icon: Banknote },
    { kind: "main_visa" as const, label: "رصيد Visa المسجل", amount: treasury.visa_minor, icon: CreditCard },
    { kind: "main_instapay" as const, label: "رصيد InstaPay المسجل", amount: treasury.instapay_minor, icon: WalletCards },
  ];
  return (
    <Card className="mb-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Building2 size={18} /> الخزنة الرئيسية</CardTitle>
        <p className="mt-1 text-xs text-slate-600">
          كاش مُورّد فعليًا بالإضافة إلى أرصدة Visa وInstaPay المسجلة. القنوات الإلكترونية ليست نقدًا في درج الخزنة ولا تؤكد التسوية البنكية. رصيد الخزنة ليس الإيراد أو الربح.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {isDemo && (
          <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
            الخزنة غير متاحة في الوضع التجريبي حالياً.
          </p>
        )}
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs font-semibold text-amber-950">
          ترتيب التفعيل: راجع Visa وInstaPay والحركات غير المرحلة أولًا، ثم سجّل الرصيد الافتتاحي الحقيقي
          لكل قناة مرة واحدة. المدفوعات السابقة لتفعيل القناة تظهر في المراجعة أدناه، ولا تُضاف تلقائيًا
          على الرصيد الافتتاحي. لا تعتمد شمولها في الافتتاحية إلا بعد المطابقة البنكية والمحاسبية.
        </p>
        <div className="rounded-xl bg-violet-50 p-4">
          <div className="text-xs font-bold text-violet-800">إجمالي الأرصدة المسجلة</div>
          <div className="mt-2 text-2xl font-black text-violet-950">{formatMoney(treasury.total_recorded_minor, treasury.currency)}</div>
          <div className="mt-1 text-xs text-violet-700">توريدات اليوم: {formatMoney(treasury.transfers_today_minor, treasury.currency)}</div>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          {channels.map(({ kind, label, amount, icon: Icon }) => (
            <div key={kind} className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center gap-2 text-xs font-semibold text-slate-600"><Icon size={15} />{label}</div>
              <div className="mt-2 text-xl font-black text-slate-950">{formatMoney(amount, treasury.currency)}</div>
              {!isDemo && !treasury.active_channels.includes(kind) && (
                <div className="mt-3 border-t border-slate-100 pt-3">
                  <p className="mb-2 text-xs text-amber-800">لم يتم تفعيل الحساب.</p>
                  <ActivationForm kind={kind} />
                </div>
              )}
            </div>
          ))}
        </div>
        {!isDemo && vaults.some((v) => !v.activated) && (
          <section className="rounded-xl border border-amber-200 p-4">
            <h3 className="mb-3 font-bold text-slate-900">تفعيل عهدة فروع الريسبشن</h3>
            <div className="grid gap-4 sm:grid-cols-2">
              {vaults.filter((v) => !v.activated).map((branch) => (
                <div key={branch.branch_id} className="rounded-lg border border-slate-100 p-3">
                  <div className="mb-2 text-sm font-semibold">{branch.branch_name}</div>
                  <ActivationForm kind="reception" branch={branch} />
                </div>
              ))}
            </div>
          </section>
        )}
        <section className="rounded-xl border border-slate-200 p-4">
          <h3 className="mb-2 flex items-center gap-2 font-bold"><ShieldCheck size={17} /> صلاحيات خزنة الفروع</h3>
          <p className="mb-4 text-xs text-slate-600">حدد أعضاء الريسبشن المصرح لهم بالتعامل مع خزنة كل فرع. الوصول للفروع الأخرى محظور.</p>
          <ActionFeedbackForm action={setReceptionBranchGrant} submitLabel="حفظ صلاحية الفرع" pendingLabel="جاري الحفظ..." refreshAfterSuccess className="grid gap-3 sm:grid-cols-3">
            <label>
              <span className="mb-1 block text-xs font-semibold text-slate-600">الفرع</span>
              <Select name="branch_id" required>
                <option value="">اختر الفرع</option>
                {vaults.map((v) => <option key={v.branch_id} value={v.branch_id}>{v.branch_name}</option>)}
              </Select>
            </label>
            <label>
              <span className="mb-1 block text-xs font-semibold text-slate-600">عضو الفريق</span>
              <Select name="user_id" required>
                <option value="">اختر العضو</option>
                {members.filter((m) => m.is_active).map((m) => (
                  <option key={m.user_id} value={m.user_id}>{m.full_name || m.email}</option>
                ))}
              </Select>
            </label>
            <label>
              <span className="mb-1 block text-xs font-semibold text-slate-600">الإجراء</span>
              <Select name="enabled" defaultValue="true">
                <option value="true">السماح</option>
                <option value="false">إلغاء الوصول</option>
              </Select>
            </label>
          </ActionFeedbackForm>
          {grants.length > 0 && (
            <p className="mt-3 text-xs text-slate-600">
              صلاحيات مفعلة: {grants.map((g) => {
                const branch = vaults.find((v) => v.branch_id === g.branch_id);
                const member = members.find((m) => m.user_id === g.user_id);
                return `${member?.full_name || member?.email || "عضو"} · ${branch?.branch_name || "فرع"}`;
              }).join(" | ")}
            </p>
          )}
        </section>
        {treasury.unattributed_payment_count > 0 && (
          <p className="rounded-xl bg-amber-50 p-3 text-sm text-amber-900" role="status">
            توجد {treasury.unattributed_payment_count} حركة دفع غير مسندة. لم تدخل أرصدة خزنة خاطئة؛ يلزم مراجعتها قبل تسوية الحسابات.
          </p>
        )}
        <PendingVaultPayments payments={pendingPayments} vaults={vaults} unavailable={pendingUnavailable} />
        <section>
          <h3 className="mb-3 flex items-center gap-2 font-bold"><Receipt size={17} /> سجل حركات الخزنة الرئيسية</h3>
          <form method="GET" className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <label><span className="mb-1 block text-xs">من</span><Input name="treasury_from" type="date" defaultValue={filters.treasury_from || ""} /></label>
            <label><span className="mb-1 block text-xs">إلى</span><Input name="treasury_to" type="date" defaultValue={filters.treasury_to || ""} /></label>
            <label><span className="mb-1 block text-xs">الفرع المصدر</span>
              <Select name="treasury_branch" defaultValue={filters.treasury_branch || ""}>
                <option value="">كل الفروع</option>
                {vaults.map((v) => <option key={v.branch_id} value={v.branch_id}>{v.branch_name}</option>)}
              </Select>
            </label>
            <label><span className="mb-1 block text-xs">طريقة الدفع</span>
              <Select name="treasury_method" defaultValue={filters.treasury_method || ""}>
                <option value="">الكل</option><option value="cash">Cash</option><option value="visa">Visa</option><option value="instapay">InstaPay</option>
              </Select>
            </label>
            <label><span className="mb-1 block text-xs">نوع الحركة</span>
              <Select name="treasury_kind" defaultValue={filters.treasury_kind || ""}>
                <option value="">الكل</option>
                {Object.entries(movementLabels).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}
              </Select>
            </label>
            <button type="submit" className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white sm:col-span-2 lg:col-span-5">تطبيق فلاتر الخزنة</button>
          </form>
          {movements.length ? movements.map((entry) => <Movement key={entry.id} entry={entry} branches={vaults} />) : (
            <p className="rounded-lg border border-dashed p-4 text-sm text-slate-500">لا توجد حركات مطابقة للفلاتر.</p>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
