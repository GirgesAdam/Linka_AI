import { Banknote, CalendarRange, CreditCard, Pencil, Plus, Receipt, Trash2, TrendingDown, TrendingUp, WalletCards } from "lucide-react";

import { ActionFeedbackForm } from "@/components/action-feedback-form";
import { ConfirmActionForm } from "@/components/confirm-action-form";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { formatMoney } from "@/lib/format";
import { canAccessSection } from "@/lib/section-access";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";

import { createExpense, deleteExpense, updateExpense } from "./actions";

type ExpenseCategory =
  | "rent"
  | "payroll"
  | "supplies"
  | "marketing"
  | "utilities"
  | "maintenance"
  | "software"
  | "taxes"
  | "other";
type ExpenseType = "fixed" | "variable";

type Expense = {
  id: string;
  title: string;
  category: ExpenseCategory;
  expense_type: ExpenseType;
  amount_minor: number;
  currency: string;
  incurred_on: string;
  note: string | null;
  created_at: string;
  updated_at: string;
};

type ProfitabilityCurrency = {
  currency: string;
  gross_payments_minor: number;
  refunds_minor: number;
  net_revenue_minor: number;
  expenses_minor: number;
  profit_minor: number;
};

type Profitability = {
  start_date: string;
  end_date: string;
  currencies: ProfitabilityCurrency[];
};

type PaymentBreakdown = {
  start_date: string;
  end_date: string;
  rows: Array<{
    payment_method: string;
    currency: string;
    amount_minor: number;
    transaction_count: number;
  }>;
};

type SearchParams = { start_date?: string; end_date?: string; notice?: string };

const categoryLabels: Record<ExpenseCategory, string> = {
  rent: "إيجار",
  payroll: "رواتب",
  supplies: "مستلزمات",
  marketing: "تسويق",
  utilities: "مرافق",
  maintenance: "صيانة",
  software: "برامج واشتراكات",
  taxes: "ضرائب ورسوم",
  other: "أخرى",
};
const expenseTypeLabels: Record<ExpenseType, string> = {
  fixed: "مصروف ثابت",
  variable: "مصروف متغير",
};
const categories = Object.entries(categoryLabels) as Array<[ExpenseCategory, string]>;
const paymentMethodLabels: Record<string, string> = {
  cash: "Cash",
  visa: "Visa",
  instapay: "InstaPay",
};

function validDate(value: string | undefined) {
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : undefined;
}

function amountInputValue(minor: number) {
  return (minor / 100).toFixed(minor % 100 === 0 ? 0 : 2);
}

function ProfitCard({ item }: { item: ProfitabilityCurrency }) {
  const profitable = item.profit_minor >= 0;
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div>
          <CardTitle>ملخص المالية</CardTitle>
          <p className="mt-1 text-xs font-semibold text-[var(--muted)]">الدخل والمصروفات وصافي الربح خلال الفترة المحددة</p>
        </div>
        <span className={`grid size-10 place-items-center rounded-xl ${profitable ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"}`}>
          {profitable ? <TrendingUp size={19} /> : <TrendingDown size={19} />}
        </span>
      </CardHeader>
      <CardContent>
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-xl bg-slate-50 p-3">
            <div className="text-[11px] font-bold text-slate-500">الدخل</div>
            <div className="mt-1 text-lg font-black text-slate-950">{formatMoney(item.net_revenue_minor, item.currency)}</div>
          </div>
          <div className="rounded-xl bg-slate-50 p-3">
            <div className="text-[11px] font-bold text-slate-500">المصروفات</div>
            <div className="mt-1 text-lg font-black text-slate-950">{formatMoney(item.expenses_minor, item.currency)}</div>
          </div>
          <div className={`rounded-xl p-3 ${profitable ? "bg-emerald-50" : "bg-rose-50"}`}>
            <div className={`text-[11px] font-bold ${profitable ? "text-emerald-700" : "text-rose-700"}`}>صافي الربح</div>
            <div className={`mt-1 text-lg font-black ${profitable ? "text-emerald-950" : "text-rose-950"}`}>{formatMoney(item.profit_minor, item.currency)}</div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function ExpenseFields({ expense, defaultDate }: { expense?: Expense; defaultDate: string }) {
  return (
    <>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <label className="xl:col-span-2">
          <span className="mb-1.5 block text-xs font-bold text-slate-600">المصروف</span>
          <Input name="title" required maxLength={200} defaultValue={expense?.title || ""} placeholder="مثال: إيجار العيادة" />
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">النوع</span>
          <Select name="expense_type" defaultValue={expense?.expense_type || "variable"}>
            <option value="fixed">ثابت</option>
            <option value="variable">متغير</option>
          </Select>
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">التصنيف</span>
          <Select name="category" defaultValue={expense?.category || "other"}>
            {categories.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">المبلغ بالجنيه</span>
          <Input name="amount" type="number" min="0.01" step="0.01" required defaultValue={expense ? amountInputValue(expense.amount_minor) : ""} placeholder="0.00" />
        </label>
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-[220px_minmax(0,1fr)]">
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">تاريخ المصروف</span>
          <Input name="incurred_on" type="date" required defaultValue={expense?.incurred_on || defaultDate} />
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">ملاحظة اختيارية</span>
          <Textarea name="note" maxLength={1000} defaultValue={expense?.note || ""} rows={2} placeholder="أي تفاصيل تساعد في المراجعة لاحقًا" />
        </label>
      </div>
    </>
  );
}

export default async function FinancePage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const raw = await searchParams;
  const startDate = validDate(raw.start_date);
  const endDate = validDate(raw.end_date);
  const noticeIsError = raw.notice === "delete_failed";
  const notice = raw.notice === "expense_deleted" ? "تم حذف المصروف بنجاح، وتم تحديث حسابات الربحية." : noticeIsError ? "تعذر حذف المصروف. حدّث الصفحة وحاول مرة أخرى." : null;
  const { workspace } = await getAppContext();
  const canManageAdministration = canAccessSection(workspace, "administration");

  const profitQuery = new URLSearchParams();
  if (startDate) profitQuery.set("start_date", startDate);
  if (endDate) profitQuery.set("end_date", endDate);
  const profitability = await tiaRequest<Profitability>(`/finance/profitability${profitQuery.size ? `?${profitQuery.toString()}` : ""}`);

  const periodQuery = new URLSearchParams({
    start_date: profitability.start_date,
    end_date: profitability.end_date,
  });
  const expenseQuery = new URLSearchParams(periodQuery);
  expenseQuery.set("limit", "500");
  const [expenses, paymentBreakdown] = await Promise.all([
    tiaRequest<Expense[]>(`/finance/expenses?${expenseQuery.toString()}`),
    tiaRequest<PaymentBreakdown>(`/finance/payment-method-breakdown?${periodQuery.toString()}`),
  ]);

  return (
    <>
      <PageHeader
        title="المالية"
        description="تابع الدخل والمصروفات وصافي الربح ودخل طرق الدفع المستخدمة حاليًا."
      />

      {notice ? <div role={noticeIsError ? "alert" : "status"} aria-live="polite" className={"mb-5 rounded-xl px-4 py-3 text-sm font-bold " + (noticeIsError ? "bg-red-50 text-red-700" : "bg-emerald-50 text-emerald-800")}>{notice}</div> : null}

      <Card className="mb-5">
        <CardContent className="pt-5">
          <form method="GET" className="flex flex-wrap items-end gap-3">
            <label>
              <span className="mb-1.5 block text-xs font-bold text-slate-600">من</span>
              <Input name="start_date" type="date" max={profitability.end_date} defaultValue={profitability.start_date} />
            </label>
            <label>
              <span className="mb-1.5 block text-xs font-bold text-slate-600">إلى</span>
              <Input name="end_date" type="date" min={profitability.start_date} defaultValue={profitability.end_date} />
            </label>
            <Button type="submit" variant="outline"><CalendarRange size={16} />تطبيق الفترة</Button>
            <div className="mr-auto text-xs font-semibold text-[var(--muted)]">{expenses.length.toLocaleString("ar-EG")} مصروف في الفترة</div>
          </form>
        </CardContent>
      </Card>

      <div className="mb-5 grid gap-4">
        {profitability.currencies.length ? profitability.currencies.map((item) => <ProfitCard key={item.currency} item={item} />) : (
          <Card><CardContent className="py-10 text-center"><WalletCards className="mx-auto text-slate-300" size={34} /><div className="mt-3 font-black text-slate-900">لا توجد حركة مالية في هذه الفترة</div></CardContent></Card>
        )}
      </div>

      <Card className="mb-5">
        <CardHeader className="flex-row items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2"><CreditCard size={17} /> الدخل حسب طريقة الدفع</CardTitle>
            <p className="mt-1 text-xs text-[var(--muted)]">يعرض Cash وVisa وInstaPay فقط. السجلات القديمة تظل محفوظة محاسبيًا ولا تظهر هنا.</p>
          </div>
        </CardHeader>
        <CardContent>
          {paymentBreakdown.rows.length ? (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {paymentBreakdown.rows.map((row) => (
                <div key={`${row.payment_method}-${row.currency}`} className="rounded-xl border border-slate-200 p-4">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-black text-slate-900">{paymentMethodLabels[row.payment_method] || row.payment_method}</div>
                    <Receipt size={15} className="text-slate-400" />
                  </div>
                  <div className="mt-2 text-xl font-black">{formatMoney(row.amount_minor, row.currency)}</div>
                  <div className="mt-1 text-xs text-[var(--muted)]">{row.transaction_count.toLocaleString("ar-EG")} دفعة</div>
                </div>
              ))}
            </div>
          ) : <div className="rounded-xl border border-dashed p-6 text-center text-sm text-[var(--muted)]">لا توجد دفعات بالطرق الحالية في الفترة المختارة.</div>}
        </CardContent>
      </Card>

      {canManageAdministration && (
        <Card className="mb-5">
          <CardHeader className="flex-row items-center justify-between gap-3">
            <div><CardTitle>إضافة مصروف</CardTitle><p className="mt-1 text-xs text-[var(--muted)]">اختر هل المصروف ثابت أو متغير وسجله وقت حدوثه.</p></div>
            <span className="grid size-9 place-items-center rounded-xl bg-teal-50 text-teal-700"><Plus size={17} /></span>
          </CardHeader>
          <CardContent>
            <ActionFeedbackForm action={createExpense} className="space-y-4" submitLabel="إضافة المصروف" pendingLabel="جاري تسجيل المصروف...">
              <ExpenseFields defaultDate={profitability.end_date} />

            </ActionFeedbackForm>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-3">
          <div><CardTitle>المصروفات</CardTitle><p className="mt-1 text-xs text-[var(--muted)]">المصروفات المسجلة بين {profitability.start_date} و{profitability.end_date}.</p></div>
          <span className="grid size-9 place-items-center rounded-xl bg-slate-100 text-slate-600"><Banknote size={17} /></span>
        </CardHeader>
        <CardContent>
          {expenses.length ? (
            <div className="space-y-3">
              {expenses.map((expense) => (
                <div key={expense.id} className="rounded-2xl border border-[var(--border)] p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div><div className="font-black text-slate-950">{expense.title}</div><div className="mt-1 text-xs font-semibold text-[var(--muted)]">{expenseTypeLabels[expense.expense_type]} · {categoryLabels[expense.category]} · {expense.incurred_on}</div>{expense.note && <div className="mt-2 text-sm text-slate-600">{expense.note}</div>}</div>
                    <div className="text-left text-lg font-black text-slate-950">{formatMoney(expense.amount_minor, expense.currency)}</div>
                  </div>
                  {canManageAdministration && (
                    <div className="mt-3 border-t border-slate-100 pt-3">
                      <details>
                        <summary className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-bold text-teal-700 hover:text-teal-800"><Pencil size={14} /> تعديل المصروف</summary>
                        <ActionFeedbackForm action={updateExpense} className="mt-4 space-y-4 rounded-xl bg-slate-50 p-4" submitLabel="حفظ التعديل" pendingLabel="جاري الحفظ..." size="sm"><input type="hidden" name="expense_id" value={expense.id} /><ExpenseFields expense={expense} defaultDate={profitability.end_date} /></ActionFeedbackForm>
                      </details>
                      <details className="mt-2">
                        <summary className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-bold text-rose-700 hover:text-rose-800"><Trash2 size={14} /> حذف المصروف</summary>
                        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-rose-50 p-3"><span className="text-xs font-semibold text-rose-900">الحذف نهائي ولن يدخل هذا المصروف في حساب الربحية بعد ذلك.</span><ConfirmActionForm action={deleteExpense} ariaLabel="حذف المصروف" confirmMessage={"هل أنت متأكد من حذف المصروف " + expense.title + "؟ الحذف نهائي وسيتم تحديث حساب الربحية."}><input type="hidden" name="expense_id" value={expense.id} /><input type="hidden" name="return_start_date" value={profitability.start_date} /><input type="hidden" name="return_end_date" value={profitability.end_date} /></ConfirmActionForm></div>
                      </details>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : <div className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-sm text-[var(--muted)]">لا توجد مصروفات مسجلة في هذه الفترة.</div>}
        </CardContent>
      </Card>
    </>
  );
}
