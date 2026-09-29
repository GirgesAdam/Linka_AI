import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatMoney } from "@/lib/format";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import { purchasePatientPackage, recordPatientPackagePayment } from "../actions";
import { PackageCancellationForm } from "./package-cancellation-form";

type PackageOffer = {
  id: string;
  service_name: string;
  device_name: string | null;
  sessions_count: number;
  price_minor: number;
  currency: string;
  savings_minor: number;
};

type PatientPackage = {
  id: string;
  name: string;
  sessions_purchased: number;
  sessions_reserved: number;
  sessions_consumed: number;
  sessions_remaining: number;
  sale_price_minor: number;
  amount_paid_minor: number;
  amount_refunded_minor: number;
  balance_due_minor: number;
  cancellation_consumed_sessions: number;
  cancellation_default_charge_minor: number | null;
  laser_device_name: string | null;
  currency: string;
  effective_status: string;
};

const statusLabels: Record<string, string> = {
  active: "نشطة",
  exhausted: "مكتملة",
  expired: "منتهية",
  cancelled: "ملغاة",
};

const statusTone: Record<string, "green" | "gray" | "red" | "yellow"> = {
  active: "green",
  exhausted: "gray",
  expired: "yellow",
  cancelled: "red",
};

function majorAmount(minor: number) {
  return (Math.max(0, minor) / 100).toFixed(2);
}

export async function PatientPackagePanel({ patientId }: { patientId: string }) {
  const [ctx, packages, offers] = await Promise.all([
    getAppContext(),
    tiaRequest<PatientPackage[]>(`/booking/patients/${patientId}/packages`),
    tiaRequest<PackageOffer[]>("/booking/package-offers?active_only=true"),
  ]);
  const canCancelPackages = ctx.workspace.role === "admin";

  return (
    <Card>
      <CardHeader>
        <CardTitle>الباكيدجات</CardTitle>
        <p className="text-xs leading-5 text-[var(--muted)]">
          الجلسات المتاحة مستقلة عن حالة الدفع. هنا الفريق يسجل البيع والدفعات كحقائق مالية فقط.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {packages.length ? (
          <div className="space-y-3">
            {packages.map((item) => (
              <div key={item.id} className="rounded-xl border border-[var(--border)] p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-bold">{item.name}</div>
                    <div className="mt-1 text-xs text-[var(--muted)]">
                      {item.laser_device_name || "بدون جهاز محدد"}
                    </div>
                  </div>
                  <Badge tone={statusTone[item.effective_status] || "gray"}>
                    {statusLabels[item.effective_status] || item.effective_status}
                  </Badge>
                </div>

                <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                  <div className="rounded-lg bg-[var(--surface-2)] p-2">
                    <div className="text-xs text-[var(--muted)]">الجلسات المتبقية</div>
                    <div className="mt-1 font-black">
                      {item.sessions_remaining} من {item.sessions_purchased}
                    </div>
                    {(item.sessions_reserved > 0 || item.sessions_consumed > 0) && (
                      <div className="mt-1 text-[11px] text-[var(--muted)]">
                        {item.sessions_consumed} مستخدمة · {item.sessions_reserved} محجوزة
                      </div>
                    )}
                  </div>
                  <div className="rounded-lg bg-[var(--surface-2)] p-2">
                    <div className="text-xs text-[var(--muted)]">الحساب</div>
                    <div className="mt-1 font-bold">
                      {formatMoney(item.amount_paid_minor, item.currency)} مدفوع من {formatMoney(item.sale_price_minor, item.currency)}
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--muted)]">
                      المتبقي {formatMoney(item.balance_due_minor, item.currency)}
                      {item.amount_refunded_minor > 0 ? ` · مرتجع ${formatMoney(item.amount_refunded_minor, item.currency)}` : ""}
                    </div>
                  </div>
                </div>

                {item.effective_status === "active" && item.balance_due_minor > 0 && (
                  <details className="mt-3 rounded-lg border border-[var(--border)] p-2">
                    <summary className="cursor-pointer text-xs font-bold text-[var(--interactive)]">تسجيل دفعة للباكيدج</summary>
                    <form action={recordPatientPackagePayment} className="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
                      <input type="hidden" name="patient_id" value={patientId} />
                      <input type="hidden" name="package_id" value={item.id} />
                      <Input
                        name="amount"
                        type="number"
                        min="0.01"
                        max={majorAmount(item.balance_due_minor)}
                        step="0.01"
                        defaultValue={majorAmount(item.balance_due_minor)}
                        required
                        aria-label="قيمة الدفعة"
                      />
                      <select name="payment_method" defaultValue="cash" className="form-control h-10 min-h-10" aria-label="طريقة الدفع">
                        <option value="cash">Cash</option>
                        <option value="visa">Visa</option>
                        <option value="instapay">InstaPay</option>
                      </select>
                      <Button type="submit" size="sm">حفظ الدفعة</Button>
                    </form>
                  </details>
                )}

                {canCancelPackages && item.effective_status === "active" && item.cancellation_default_charge_minor != null && (
                  <PackageCancellationForm
                    patientId={patientId}
                    packageId={item.id}
                    currency={item.currency}
                    consumedSessions={item.cancellation_consumed_sessions}
                    defaultChargeMinor={item.cancellation_default_charge_minor}
                    amountPaidMinor={item.amount_paid_minor}
                    amountRefundedMinor={item.amount_refunded_minor}
                  />
                )}
                {canCancelPackages && item.effective_status === "active" && item.cancellation_default_charge_minor == null && (
                  <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs font-semibold text-amber-900">
                    لا يمكن حساب تسوية الإلغاء تلقائيًا للباكيدج القديمة دي لأن بيانات السعر أو عدد الجلسات الأصلي غير مكتملة.
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed border-[var(--border)] p-4 text-sm text-[var(--muted)]">
            لا توجد باكيدجات مسجلة لهذا العميل حتى الآن.
          </div>
        )}

        <details className="rounded-xl border border-[var(--border)] p-3">
          <summary className="cursor-pointer text-sm font-bold text-slate-800">بيع باكيدج جديدة</summary>
          {offers.length ? (
            <form action={purchasePatientPackage} className="mt-4 space-y-3">
              <input type="hidden" name="patient_id" value={patientId} />
              <label className="block text-xs font-bold text-slate-700">
                العرض
                <select name="offer_id" required className="form-control mt-1 h-10 min-h-10" defaultValue="">
                  <option value="" disabled>اختار الباكيدج</option>
                  {offers.map((offer) => (
                    <option key={offer.id} value={offer.id}>
                      {offer.service_name}{offer.device_name ? ` · ${offer.device_name}` : ""} · {offer.sessions_count} جلسات · {formatMoney(offer.price_minor, offer.currency)}
                    </option>
                  ))}
                </select>
              </label>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-xs font-bold text-slate-700">
                  دفعة الآن
                  <Input name="initial_payment" type="number" min="0" step="0.01" defaultValue="0" className="mt-1" />
                  <span className="mt-1 block font-normal leading-5 text-[var(--muted)]">اكتب 0 لو الدفع هيتسجل بعدين.</span>
                </label>
                <label className="block text-xs font-bold text-slate-700">
                  طريقة الدفع
                  <select name="payment_method" defaultValue="cash" className="form-control mt-1 h-10 min-h-10">
                    <option value="cash">Cash</option>
                    <option value="visa">Visa</option>
                    <option value="instapay">InstaPay</option>
                  </select>
                </label>
              </div>
              <Button type="submit" className="w-full">تسجيل الباكيدج</Button>
            </form>
          ) : (
            <p className="mt-3 text-xs leading-5 text-[var(--muted)]">
              مفيش عروض باكيدجات مفعلة. أضف باكيدج بعدد الجلسات والسعر المناسب من صفحة الخدمات أولًا.
            </p>
          )}
        </details>
      </CardContent>
    </Card>
  );
}
