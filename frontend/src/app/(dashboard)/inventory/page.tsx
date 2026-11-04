import { CheckCircle2, CircleAlert, PackagePlus, ShoppingBag, Syringe } from "lucide-react";

import { ActionFeedbackForm } from "@/components/action-feedback-form";
import { ConfirmActionForm } from "@/components/confirm-action-form";
import { PageHeader } from "@/components/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";

import {
  addClinicProductStock,
  addInventoryStock,
  createClinicProduct,
  createInventoryItem,
  deleteClinicProduct,
  deleteInventoryItem,
  recordInventoryUsage,
} from "./actions";

type Item = {
  id: string;
  name: string;
  quantity_ml: number | string;
  low_stock_threshold_ml: number | string | null;
  notes: string | null;
};

type Product = {
  id: string;
  name: string;
  description: string | null;
  quantity_on_hand: number;
  is_active: boolean;
};

type InventoryPageProps = {
  searchParams: Promise<{ notice?: string }>;
};

const noticeMessages: Record<string, string> = {
  product_deleted: "تم حذف المنتج من المخزن. سجلات المواعيد السابقة محفوظة.",
  item_deleted: "تم حذف الحقنة من المخزن. سجل الاستخدام السابق محفوظ.",
  delete_failed: "تعذر حذف العنصر. حدّث الصفحة وحاول مرة أخرى.",
};

export default async function InventoryPage({ searchParams }: InventoryPageProps) {
  const [{ workspace }, items, products, params] = await Promise.all([
    getAppContext(),
    tiaRequest<Item[]>("/inventory/items"),
    tiaRequest<Product[]>("/inventory/products").catch(() => []),
    searchParams,
  ]);
  const isAdmin = workspace.role === "admin";
  const notice = params.notice ? noticeMessages[params.notice] : null;
  const noticeIsError = params.notice === "delete_failed";

  return (
    <>
      <PageHeader
        title="المخزن"
        description="تابع كميات المنتجات والحقن بشكل مباشر، وسجّل الإضافة والاستخدام."
      />

      {notice ? (
        <div role={noticeIsError ? "alert" : "status"} aria-live="polite" className={"mb-5 flex items-center gap-2 rounded-xl px-4 py-3 text-sm font-bold " + (noticeIsError ? "bg-red-50 text-red-700" : "bg-emerald-50 text-emerald-800")}>
          {noticeIsError ? <CircleAlert size={17} /> : <CheckCircle2 size={17} />}
          {notice}
        </div>
      ) : null}

      <Card className="mb-5">
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><ShoppingBag size={18} /> منتجات العيادة</CardTitle>
          <p className="mt-1 text-xs text-[var(--muted)]">
            العدد هو رصيد المخزن الحالي. عند إضافة منتج لموعد يُخصم العدد تلقائيًا، وعند حذفه من الموعد يرجع للمخزن.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          {isAdmin ? (
            <ActionFeedbackForm
              action={createClinicProduct}
              className="grid gap-3 md:grid-cols-[minmax(180px,1fr)_minmax(220px,2fr)_120px_auto]"
              submitLabel="إضافة المنتج"
              pendingLabel="جاري الإضافة..."
            >
              <label><span className="mb-1.5 block text-xs font-bold">اسم المنتج</span><Input name="name" required maxLength={180} placeholder="مثال: Skin Protector" /></label>
              <label><span className="mb-1.5 block text-xs font-bold">وصف اختياري</span><Input name="description" maxLength={500} placeholder="معلومة قصيرة للاستقبال" /></label>
              <label><span className="mb-1.5 block text-xs font-bold">العدد عند إنشاء المنتج</span><Input name="quantity_on_hand" type="number" min="0" step="1" defaultValue="0" required /></label>
            </ActionFeedbackForm>
          ) : null}

          {products.length ? (
            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table className="data-table min-w-[760px]">
                <thead>
                  <tr><th>المنتج</th><th>الوصف</th><th>العدد المتاح</th><th>إضافة كمية جديدة</th>{isAdmin ? <th>حذف</th> : null}</tr>
                </thead>
                <tbody>
                  {products.map((product) => (
                    <tr key={product.id}>
                      <td className="font-black">{product.name}</td>
                      <td>{product.description || "—"}</td>
                      <td className="font-black">{product.quantity_on_hand.toLocaleString("ar-EG")}</td>
                      <td>
                        {isAdmin ? (
                          <ActionFeedbackForm
                            action={addClinicProductStock}
                            className="flex flex-wrap items-center gap-2"
                            submitLabel="إضافة"
                            pendingLabel="..."
                            variant="outline"
                            size="sm"
                          >
                            <input type="hidden" name="product_id" value={product.id} />
                            <Input name="quantity_to_add" type="number" min="1" step="1" required placeholder="العدد" className="w-24" />
                          </ActionFeedbackForm>
                        ) : "—"}
                      </td>
                      {isAdmin ? (
                        <td>
                          <ConfirmActionForm
                            action={deleteClinicProduct}
                            className="flex items-center"
                            ariaLabel={"حذف المنتج " + product.name}
                            confirmMessage={"هل تريد حذف المنتج " + product.name + " من المخزن؟ سيختفي من الإضافات الجديدة، وستظل سجلات المواعيد السابقة محفوظة."}
                          >
                            <input type="hidden" name="product_id" value={product.id} />
                          </ConfirmActionForm>
                        </td>
                      ) : null}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-slate-300 p-6 text-center text-sm text-[var(--muted)]">لا توجد منتجات بعد.</div>
          )}
        </CardContent>
      </Card>

      {isAdmin ? (
        <Card className="mb-5">
          <CardHeader><CardTitle className="flex items-center gap-2"><PackagePlus size={18} /> إضافة حقنة للمخزن</CardTitle></CardHeader>
          <CardContent>
            <ActionFeedbackForm
              action={createInventoryItem}
              className="grid gap-3 md:grid-cols-3"
              submitLabel="إضافة الحقنة"
              pendingLabel="جاري الإضافة..."
            >
              <label><span className="mb-1.5 block text-xs font-bold">الاسم</span><Input name="name" required placeholder="مثال: Botox vial" /></label>
              <label><span className="mb-1.5 block text-xs font-bold">الكمية الحالية mL</span><Input name="quantity_ml" type="number" min="0" step="0.001" required /></label>
              <label><span className="mb-1.5 block text-xs font-bold">تنبيه المخزون عند mL</span><Input name="low_stock_threshold_ml" type="number" min="0" step="0.001" /></label>
              <label className="md:col-span-2"><span className="mb-1.5 block text-xs font-bold">ملاحظة</span><Textarea name="notes" rows={2} /></label>
            </ActionFeedbackForm>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-2">
        {items.map((item) => {
          const quantityMl = Number(item.quantity_ml);
          const threshold = item.low_stock_threshold_ml == null ? null : Number(item.low_stock_threshold_ml);
          const low = threshold != null && quantityMl <= threshold;
          return (
            <Card key={item.id}>
              <CardHeader className="flex-row items-start justify-between gap-3">
                <div><CardTitle className="flex items-center gap-2"><Syringe size={17} />{item.name}</CardTitle></div>
                <div className="flex items-center gap-2">
                  <span className={"rounded-full px-2.5 py-1 text-xs font-bold " + (low ? "bg-rose-50 text-rose-700" : "bg-emerald-50 text-emerald-700")}>{low ? "مخزون منخفض" : "متاح"}</span>
                  {isAdmin ? (
                    <ConfirmActionForm
                      action={deleteInventoryItem}
                      className="flex items-center"
                      ariaLabel={"حذف الحقنة " + item.name}
                      confirmMessage={"هل تريد حذف الحقنة " + item.name + " من المخزن؟ سيظل سجل الاستخدام السابق محفوظًا."}
                    >
                      <input type="hidden" name="item_id" value={item.id} />
                    </ConfirmActionForm>
                  ) : null}
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="rounded-xl bg-slate-50 p-3"><div className="text-[11px] font-bold text-slate-500">المتبقي</div><div className="mt-1 text-xl font-black">{quantityMl.toLocaleString("ar-EG")} mL</div></div>
                {isAdmin ? (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <ActionFeedbackForm
                      action={addInventoryStock}
                      className="flex flex-wrap items-end gap-2 rounded-xl border p-3"
                      submitLabel="إضافة"
                      pendingLabel="..."
                      variant="outline"
                      size="sm"
                    >
                      <input type="hidden" name="item_id" value={item.id} />
                      <div className="w-full text-xs font-black">إضافة كمية جديدة</div>
                      <Input name="quantity_ml" type="number" min="0.001" step="0.001" required placeholder="mL" />
                    </ActionFeedbackForm>
                    <ActionFeedbackForm
                      action={recordInventoryUsage}
                      className="flex flex-wrap items-end gap-2 rounded-xl border p-3"
                      submitLabel="خصم"
                      pendingLabel="..."
                      size="sm"
                    >
                      <input type="hidden" name="item_id" value={item.id} />
                      <div className="w-full text-xs font-black">تسجيل استخدام</div>
                      <Input name="used_ml" type="number" min="0.001" step="0.001" required placeholder="mL مستخدمة" />
                    </ActionFeedbackForm>
                  </div>
                ) : null}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </>
  );
}