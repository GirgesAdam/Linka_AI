import Link from "next/link";
import {
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  CircleDollarSign,
  CircleX,
  History,
  PackagePlus,
  ReceiptText,
  Stethoscope,
  Trash2,
  UserRound,
  UserX,
  Workflow,
} from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDateTime, formatMoney } from "@/lib/format";
import { appointmentLabels, labelForStatus, toneForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import type {
  AppointmentOperationsDetail,
  AppointmentPaymentSummary,
  AppointmentPulseSettlement,
  Doctor,
  PulseBalance,
  PulseBillingSettings,
  PulsePackOffer,
  PatientPackage,
  Staff,
} from "@/lib/types";
import {
  addAppointmentProduct,
  cancelAppointment,
  confirmAppointment,
  purchasePackageForAdditionalService,
  purchasePackageFromAppointment,
  refundAppointmentPayment,
  removeAppointmentAdditionalService,
  removeAppointmentProduct,
  updateAppointmentStatus,
  updateLaserPulses,
} from "./actions";
import { AdditionalServiceForm } from "./additional-service-form";
import { AppointmentPaymentForm } from "./appointment-payment-form";
import {
  AppointmentServiceEditor,
  type AppointmentDevicePrice,
  type AppointmentServiceOption,
} from "./service-editor";

const paymentMethodLabels: Record<string, string> = {
  cash: "Cash",
  visa: "Visa",
  instapay: "InstaPay",
  card: "بطاقة - سجل قديم",
  bank_transfer: "تحويل بنكي - سجل قديم",
  wallet: "محفظة إلكترونية - سجل قديم",
  online: "دفع إلكتروني - سجل قديم",
  other: "طريقة قديمة أخرى",
  unknown: "غير محدد",
};

type ClinicProduct = {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
};

type AppointmentProductLine = {
  id: string;
  appointment_id: string;
  product_id: string;
  product_name: string;
  quantity: number;
  unit_price_minor: number;
  currency: string;
  total_minor: number;
};

type PaymentSummaryWithProducts = AppointmentPaymentSummary & {
  subtotal_minor?: number;
  discount_minor?: number;
  service_price_minor?: number;
  products_total_minor?: number;
  additional_services_total_minor?: number;
  package_sales_total_minor?: number;
  pulse_pack_sales_total_minor?: number;
  pulse_overage_total_minor?: number;
};

type AppointmentAdditionalService = {
  id: string;
  appointment_id: string;
  service_id: string;
  service_name: string;
  unit_price_minor: number;
  currency: string;
  laser_device_key: string | null;
  laser_device_name: string | null;
  patient_package_id: string | null;
  billing_context: string;
  laser_pulses_used: number | null;
  pulse_resolution: string | null;
  pulse_resolution_pulse_pack_id: string | null;
  pulse_overage_unit_price_minor: number | null;
  pulse_overage_charge_minor: number;
};

type PackageOffer = {
  id: string;
  service_id: string;
  service_name: string;
  device_key: string;
  device_name: string;
  sessions_count: number;
  price_minor: number;
  currency: string;
};

function minorInput(value: number) {
  return (value / 100).toFixed(2);
}

export default async function AppointmentOperationsPage({
  params,
  searchParams,
}: {
  params: Promise<{ appointmentId: string }>;
  searchParams: Promise<{ visit_error?: string; visit_saved?: string }>;
}) {
  const { appointmentId } = await params;
  const feedback = await searchParams;
  const [
    detail,
    payments,
    products,
    productLines,
    services,
    devicePrices,
    doctors,
    staff,
    additionalServices,
    packageOffers,
    pulseSettlement,
    pulsePackOffers,
    pulseSettings,
  ] = await Promise.all([
    tiaRequest<AppointmentOperationsDetail>(`/booking/appointments/${appointmentId}/operations`),
    tiaRequest<PaymentSummaryWithProducts>(`/payments/appointments/${appointmentId}`),
    tiaRequest<ClinicProduct[]>("/inventory/products").catch(() => []),
    tiaRequest<AppointmentProductLine[]>(`/inventory/appointments/${appointmentId}/products`).catch(() => []),
    tiaRequest<AppointmentServiceOption[]>("/clinic/services").catch(() => []),
    tiaRequest<AppointmentDevicePrice[]>("/inventory/laser-prices").catch(() => []),
    tiaRequest<Doctor[]>("/clinic/doctors").catch(() => []),
    tiaRequest<Staff[]>("/clinic/staff").catch(() => []),
    tiaRequest<AppointmentAdditionalService[]>(`/booking/appointments/${appointmentId}/additional-services`).catch(() => []),
    tiaRequest<PackageOffer[]>("/booking/package-offers?active_only=true").catch(() => []),
    tiaRequest<AppointmentPulseSettlement | null>(`/booking/appointments/${appointmentId}/pulse-settlement`).catch(() => null),
    tiaRequest<PulsePackOffer[]>("/booking/pulse-pack-offers?active_only=true").catch(() => []),
    tiaRequest<PulseBillingSettings[]>("/booking/pulse-device-prices").catch(() => []),
  ]);
  const { appointment } = detail;
  const [pulseBalances, patientPackages] = await Promise.all([
    tiaRequest<PulseBalance[]>(
      `/booking/patients/${appointment.patient_id}/pulse-balance`,
    ).catch(() => []),
    tiaRequest<PatientPackage[]>(
      `/booking/patients/${appointment.patient_id}/packages`,
    ).catch(() => []),
  ]);
  const allowed = new Set(detail.allowed_actions);
  const laserAppointment = appointment as typeof appointment & {
    laser_device_key?: string | null;
    laser_device_name?: string | null;
    laser_pulses_used?: number | null;
  };
  const canAddProducts = !["cancelled", "no_show", "rescheduled"].includes(appointment.status);
  const canEditService = ["pending", "confirmed", "checked_in", "in_progress"].includes(appointment.status);
  const packageBacked = Boolean(
    appointment.patient_package_id || appointment.billing_context === "package_prepaid" || appointment.package_external_id,
  );
  const patientPackageMap = new Map(patientPackages.map((item) => [item.id, item]));
  const primaryPackage = appointment.patient_package_id
    ? patientPackageMap.get(appointment.patient_package_id) ?? null
    : appointment.package_external_id
      ? patientPackages.find((item) => item.external_id === appointment.package_external_id) ?? null
      : null;
  const pulseBacked = appointment.billing_context === "pulse_prepaid";
  const prepaidBacked = packageBacked || pulseBacked;
  const pulseSettlementPending =
    pulseBacked && (!pulseSettlement || pulseSettlement.resolution === "pending");
  const pulseDeviceSetting = pulseSettings.find(
    (setting) => setting.device_key === laserAppointment.laser_device_key,
  );
  const patientPulseBalance =
    pulseBalances.find((balance) => balance.device_key === laserAppointment.laser_device_key)
      ?.pulses_remaining ?? 0;
  const visiblePulseBalances = [...pulseBalances];
  if (
    laserAppointment.laser_device_key &&
    !visiblePulseBalances.some(
      (balance) => balance.device_key === laserAppointment.laser_device_key,
    )
  ) {
    visiblePulseBalances.unshift({
      device_key: laserAppointment.laser_device_key,
      device_name: laserAppointment.laser_device_name || laserAppointment.laser_device_key,
      pulses_purchased: 0,
      pulses_consumed: 0,
      pulses_remaining: 0,
      active_pack_count: 0,
    });
  }
  const checkoutPulseDeficit = pulseSettlement
    ? pulseSettlement.deficit_pulses
    : Math.max((laserAppointment.laser_pulses_used ?? 0) - patientPulseBalance, 0);
  const compatiblePulsePackOffers = pulsePackOffers.filter(
    (offer) =>
      offer.device_key === laserAppointment.laser_device_key &&
      offer.pulses_count >= checkoutPulseDeficit,
  );
  const overpaidMinor = Math.max(payments.net_paid_minor - payments.price_minor, 0);
  const canEditVisitCharges = ["pending", "confirmed", "checked_in", "in_progress", "completed"].includes(appointment.status);
  const compatiblePackageOffers = packageOffers.filter(
    (offer) =>
      offer.service_id === appointment.service_id &&
      (!laserAppointment.laser_device_key || offer.device_key === laserAppointment.laser_device_key),
  );
  const staffMap = new Map(staff.map((item) => [item.id, `${item.first_name} ${item.last_name}`.trim()]));
  const doctorOptions = doctors
    .filter((item) => item.is_active || item.id === appointment.doctor_id)
    .map((item) => ({ id: item.id, name: staffMap.get(item.staff_id) || "دكتور" }));

  return (
    <>
      <PageHeader
        title={detail.patient.name}
        description={`${detail.service.name} · ${formatDateTime(appointment.start_at)}`}
        action={
          <Link href="/appointments" className={buttonVariants({ variant: "outline" })}>
            <ArrowRight size={15} /> المواعيد
          </Link>
        }
      />

      {feedback.visit_error && (
        <div role="alert" className="mb-4 rounded-xl border border-red-200 bg-red-50 p-3 text-sm font-bold text-red-800">
          {feedback.visit_error}
        </div>
      )}
      {feedback.visit_saved && !feedback.visit_error && (
        <div className="mb-4 rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-3 text-sm font-bold text-[var(--accent-strong)]">
          {feedback.visit_saved === "package"
            ? "تمت إضافة الباكيدج للحساب واحتساب الجلسة الحالية منها."
            : feedback.visit_saved === "extra_package"
              ? "تمت إضافة الباكيدج للخدمة الإضافية واحتسابها كجلسة منها."
              : feedback.visit_saved === "pulse_usage"
                ? "تم حفظ استهلاك الـPulses الفعلي."
                : feedback.visit_saved === "pulse_overage"
                  ? "تمت إضافة تكلفة الـPulses الزائدة إلى حساب الموعد."
                  : feedback.visit_saved === "pulse_pack"
                    ? "تمت إضافة باقة Pulses جديدة وتغطية العجز منها."
                    : feedback.visit_saved === "payment"
                      ? "تم تسجيل الحساب والدفعة بنجاح."
                      : "تم تحديث خدمات الزيارة والحساب."}
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="space-y-5">
          <Card>
            <CardHeader className="flex-row items-start justify-between gap-3">
              <div>
                <CardTitle>تفاصيل الموعد</CardTitle>
                <div className="mt-1 text-xs text-[var(--muted)]">أهم بيانات الموعد والإجراء المناسب لحالته الحالية.</div>
              </div>
              <StatusBadge domain="appointment" status={appointment.status} />
            </CardHeader>
            <CardContent>
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-xl bg-[var(--surface-2)] p-4">
                  <div className="flex items-center gap-2 text-xs text-[var(--muted)]"><UserRound size={14} /> العميل</div>
                  <Link href={`/patients/${detail.patient.id}`} className="mt-1 block font-black text-[var(--accent-strong)] hover:underline">{detail.patient.name}</Link>
                  <div className="mt-1 text-xs text-[var(--muted)]" dir="ltr">{detail.patient.phone || "—"}</div>
                </div>
                <div className="rounded-xl bg-[var(--surface-2)] p-4">
                  <div className="flex items-center gap-2 text-xs text-[var(--muted)]"><CalendarClock size={14} /> الموعد</div>
                  <div className="mt-1 font-black">{formatDateTime(appointment.start_at)}</div>
                  <div className="mt-1 text-xs text-[var(--muted)]">{appointment.duration_minutes} دقيقة</div>
                </div>
                <div className="rounded-xl bg-[var(--surface-2)] p-4">
                  <div className="flex items-center gap-2 text-xs text-[var(--muted)]"><Stethoscope size={14} /> الخدمة والطبيب</div>
                  <div className="mt-1 font-black">{detail.service.name}</div>
                  <div className="mt-1 text-xs text-[var(--muted)]">{detail.doctor.name} · {formatMoney(payments.service_price_minor ?? appointment.price_minor, appointment.currency)}</div>
                  {laserAppointment.laser_device_name && <div className="mt-1 text-xs font-bold text-[var(--accent-strong)]">الجهاز: {laserAppointment.laser_device_name}</div>}
                </div>
              </div>

              {laserAppointment.laser_device_key && (
                <div className="mt-4 space-y-3">
                  <form action={updateLaserPulses} className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-3">
                    <input type="hidden" name="appointment_id" value={appointment.id} />
                    <input type="hidden" name="patient_id" value={appointment.patient_id} />
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
                      <label className="min-w-0 flex-1 text-xs font-bold text-slate-700">
                        عدد الـPulses المستخدمة فعليًا
                        <Input name="pulses_used" type="number" min="0" max="10000000" step="1" required
                          defaultValue={laserAppointment.laser_pulses_used ?? ""} placeholder="مثال: 2350" className="mt-1" />
                        <span className="mt-1 block font-normal text-[var(--muted)]">
                          {pulseBacked
                            ? "اكتب الرقم من الجهاز بعد الجلسة. السيستم هيخصم المتاح من الرصيد ويحسب أي عجز تلقائيًا."
                            : "للمتابعة التشغيلية فقط، ولا يغيّر سعر الجلسة أو مدتها."}
                        </span>
                      </label>
                      <Button type="submit" variant="outline">حفظ الاستهلاك</Button>
                    </div>
                  </form>

                  {pulseBacked && pulseSettlement && (
                    <div className={`rounded-xl border p-4 ${
                      pulseSettlement.resolution === "pending"
                        ? "border-amber-200 bg-amber-50"
                        : "border-emerald-200 bg-emerald-50/60"
                    }`}>
                      <div className="grid gap-3 sm:grid-cols-3">
                        <div>
                          <div className="text-xs text-[var(--muted)]">الاستهلاك الفعلي</div>
                          <b className="mt-1 block">{pulseSettlement.pulses_used.toLocaleString("ar-EG")} Pulse</b>
                        </div>
                        <div>
                          <div className="text-xs text-[var(--muted)]">من الرصيد</div>
                          <b className="mt-1 block">{pulseSettlement.pulses_from_balance.toLocaleString("ar-EG")} Pulse</b>
                        </div>
                        <div>
                          <div className="text-xs text-[var(--muted)]">المتبقي بعد التسوية</div>
                          <b className="mt-1 block">{pulseSettlement.available_balance_after.toLocaleString("ar-EG")} Pulse</b>
                        </div>
                      </div>

                      {pulseSettlement.resolution === "pending" && pulseSettlement.deficit_pulses > 0 && (
                        <div className="mt-4 border-t border-amber-200 pt-4">
                          <div className="text-sm font-black text-amber-950">
                            الرصيد لا يكفي — العجز {pulseSettlement.deficit_pulses.toLocaleString("ar-EG")} Pulse
                          </div>
                          <p className="mt-1 text-xs leading-5 text-amber-900">
                            سوّي العجز من قسم المدفوعات بالأسفل: شراء باقة Pulses أو دفع الـPulses غير المغطاة.
                          </p>
                        </div>
                      )}

                      {pulseSettlement.resolution === "overage" && (
                        <div className="mt-3 text-xs font-bold text-emerald-900">
                          تم تحويل {pulseSettlement.deficit_pulses.toLocaleString("ar-EG")} Pulse زائدة إلى حساب الموعد بقيمة {formatMoney(pulseSettlement.overage_charge_minor, pulseSettlement.currency)}.
                        </div>
                      )}
                      {pulseSettlement.resolution === "new_pack" && (
                        <div className="mt-3 text-xs font-bold text-emerald-900">
                          تم تغطية العجز من باقة Pulses جديدة، والباقي منها متاح للجلسات القادمة.
                        </div>
                      )}
                    </div>
                  )}

                  {pulseBacked && !pulseSettlement && (
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-700">
                      الجلسة محسوبة من رصيد الـPulses. سجّل الاستهلاك الفعلي بعد الجلسة عشان السيستم يخصم الرصيد ويكشف أي عجز.
                    </div>
                  )}
                </div>
              )}

              <div className="mt-5 flex flex-wrap gap-2 border-t border-[var(--border)] pt-4">
                {allowed.has("confirm") && (
                  <form action={confirmAppointment}>
                    <input type="hidden" name="appointment_id" value={appointment.id} />
                    <input type="hidden" name="patient_id" value={appointment.patient_id} />
                    <Button><CheckCircle2 size={15} /> تأكيد الموعد</Button>
                  </form>
                )}
                {allowed.has("complete") && (
                  pulseSettlementPending ? (
                    <div className="flex flex-col gap-1">
                      <Button disabled><CheckCircle2 size={15} /> تسجيل اكتمال الجلسة</Button>
                      <span className="text-[11px] font-semibold text-amber-700">
                        سجّل استهلاك الـPulses وسوّي أي عجز أولًا.
                      </span>
                    </div>
                  ) : (
                    <form action={updateAppointmentStatus}>
                      <input type="hidden" name="appointment_id" value={appointment.id} />
                      <input type="hidden" name="patient_id" value={appointment.patient_id} />
                      <input type="hidden" name="status" value="completed" />
                      <Button><CheckCircle2 size={15} /> تسجيل اكتمال الجلسة</Button>
                    </form>
                  )
                )}
                {allowed.has("no_show") && (
                  <form action={updateAppointmentStatus}>
                    <input type="hidden" name="appointment_id" value={appointment.id} />
                    <input type="hidden" name="patient_id" value={appointment.patient_id} />
                    <input type="hidden" name="status" value="no_show" />
                    <Button variant="ghost"><UserX size={15} /> تسجيل عدم الحضور</Button>
                  </form>
                )}
              </div>

              {canEditService && services.length > 0 && (
                <AppointmentServiceEditor
                  key={`${appointment.service_id}:${appointment.doctor_id}:${laserAppointment.laser_device_key || ""}:${appointment.start_at}`}
                  appointmentId={appointment.id}
                  patientId={appointment.patient_id}
                  branchId={appointment.branch_id}
                  currentStartAt={appointment.start_at}
                  timezone={detail.timezone}
                  currentServiceId={appointment.service_id}
                  currentDoctorId={appointment.doctor_id}
                  currentDeviceKey={laserAppointment.laser_device_key || null}
                  packageBacked={prepaidBacked}
                  services={services}
                  doctors={doctorOptions}
                  devicePrices={devicePrices}
                />
              )}

              {allowed.has("cancel") && (
                <details className="mt-4 rounded-xl border border-red-100 bg-red-50/40 p-3">
                  <summary className="cursor-pointer text-sm font-bold text-red-700">إلغاء الموعد</summary>
                  <div className="mt-3">
                    {detail.cancellation_override_required && !detail.can_override_cancellation_policy ? (
                      <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">الموعد داخل مهلة منع الإلغاء، لذلك يحتاج مدير العيادة إلى تنفيذ الإلغاء.</div>
                    ) : (
                      <form action={cancelAppointment} className="space-y-3">
                        <input type="hidden" name="appointment_id" value={appointment.id} />
                        <input type="hidden" name="patient_id" value={appointment.patient_id} />
                        <label className="block text-sm font-bold">سبب الإلغاء<input name="reason" required maxLength={2000} placeholder="مثال: طلب العميل إلغاء الموعد" className="form-control mt-2 h-10 min-h-10" /></label>
                        {detail.cancellation_override_required && <label className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm"><input type="checkbox" name="override_policy" value="1" required className="mt-1" />تأكيد الإلغاء رغم تجاوز مهلة الإلغاء المسموح بها</label>}
                        <Button variant="danger"><CircleX size={15} /> تأكيد الإلغاء</Button>
                      </form>
                    )}
                  </div>
                </details>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-3">
                <CardTitle className="flex items-center gap-2"><CircleDollarSign size={17} /> المدفوعات</CardTitle>
                {payments.balance_minor > 0 && <Badge tone="yellow">متبقي {formatMoney(payments.balance_minor, payments.currency)}</Badge>}
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {(visiblePulseBalances.length > 0 || primaryPackage) && (
                <div className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-4">
                  <div className="text-sm font-black text-slate-900">رصيد العميل وقت الحساب</div>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {visiblePulseBalances.map((balance) => (
                      <div key={balance.device_key} className="rounded-lg bg-white p-3">
                        <div className="text-xs text-[var(--muted)]">{balance.device_name}</div>
                        <b className="mt-1 block text-slate-900">
                          {balance.pulses_remaining.toLocaleString("ar-EG")} Pulse متبقية
                        </b>
                        {balance.active_pack_count > 0 && (
                          <div className="mt-1 text-[11px] text-[var(--muted)]">
                            من {balance.active_pack_count.toLocaleString("ar-EG")} باقة نشطة
                          </div>
                        )}
                      </div>
                    ))}
                    {primaryPackage && (
                      <div className="rounded-lg bg-white p-3">
                        <div className="text-xs text-[var(--muted)]">{primaryPackage.name}</div>
                        <b className="mt-1 block text-slate-900">
                          {primaryPackage.sessions_remaining.toLocaleString("ar-EG")} جلسة متبقية
                        </b>
                        <div className="mt-1 text-[11px] text-[var(--muted)]">
                          بعد احتساب الحجوزات الحالية من أصل {primaryPackage.sessions_purchased.toLocaleString("ar-EG")} جلسة
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}

              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                <div className="rounded-xl bg-[var(--surface-2)] p-3">
                  <div className="text-xs text-[var(--muted)]">الخدمة الأساسية</div>
                  <b className="mt-1 block">
                    {packageBacked
                      ? "ضمن الباكيدج"
                      : pulseBacked
                        ? "من رصيد الـPulses"
                        : formatMoney(payments.service_price_minor ?? appointment.price_minor, payments.currency)}
                  </b>
                </div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">خدمات إضافية</div><b className="mt-1 block">{formatMoney(payments.additional_services_total_minor ?? 0, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">باكيدجات الجلسات</div><b className="mt-1 block">{formatMoney(payments.package_sales_total_minor ?? 0, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">باقات Pulses</div><b className="mt-1 block">{formatMoney(payments.pulse_pack_sales_total_minor ?? 0, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">Pulses إضافية</div><b className="mt-1 block">{formatMoney(payments.pulse_overage_total_minor ?? 0, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">المنتجات</div><b className="mt-1 block">{formatMoney(payments.products_total_minor ?? 0, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">قبل الخصم</div><b className="mt-1 block">{formatMoney(payments.subtotal_minor ?? payments.price_minor, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--accent-soft)] p-3"><div className="text-xs text-[var(--accent-strong)]">الخصم</div><b className="mt-1 block text-[var(--accent-strong)]">{formatMoney(payments.discount_minor ?? 0, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">الإجمالي بعد الخصم</div><b className="mt-1 block">{formatMoney(payments.price_minor, payments.currency)}</b></div>
              </div>

              <details className="rounded-xl border border-slate-200 p-3" open={additionalServices.length > 0 ? true : undefined}>
                <summary className="flex cursor-pointer items-center gap-2 text-sm font-black text-slate-900"><Stethoscope size={16} /> خدمات إضافية أثناء الزيارة</summary>
                <div className="mt-4 space-y-3">
                  {additionalServices.length > 0 && (
                    <div className="space-y-2">
                      {additionalServices.map((line) => {
                        const linePackageOffers = packageOffers.filter(
                          (offer) =>
                            offer.service_id === line.service_id &&
                            (!line.laser_device_key || offer.device_key === line.laser_device_key),
                        );
                        const linePackage = line.patient_package_id
                          ? patientPackageMap.get(line.patient_package_id) ?? null
                          : null;
                        return (
                          <div key={line.id} className="rounded-xl bg-slate-50 p-3">
                            <div className="flex flex-wrap items-start justify-between gap-3">
                              <div>
                                <div className="flex flex-wrap items-center gap-2">
                                  <div className="text-sm font-black">{line.service_name}</div>
                                  {line.patient_package_id && <Badge tone="green">ضمن باكيدج</Badge>}
                                  {line.billing_context === "pulse_prepaid" && <Badge tone="green">من Pulses</Badge>}
                                </div>
                                <div className="mt-1 text-xs text-[var(--muted)]">
                                  {line.laser_device_name ? `${line.laser_device_name} · ` : ""}
                                  {line.patient_package_id
                                    ? `من الباكيدج · ${(linePackage?.sessions_remaining ?? 0).toLocaleString("ar-EG")} جلسة متبقية`
                                    : line.billing_context === "pulse_prepaid"
                                      ? `${(line.laser_pulses_used ?? 0).toLocaleString("ar-EG")} Pulse · سعر الخدمة الفردية غير محسوب`
                                      : formatMoney(line.unit_price_minor, line.currency)}
                                </div>
                              </div>
                              {canEditVisitCharges && !line.patient_package_id && line.billing_context !== "pulse_prepaid" && (
                                <form action={removeAppointmentAdditionalService}>
                                  <input type="hidden" name="appointment_id" value={appointment.id} />
                                  <input type="hidden" name="patient_id" value={appointment.patient_id} />
                                  <input type="hidden" name="line_id" value={line.id} />
                                  <Button size="sm" variant="ghost" aria-label={`حذف ${line.service_name}`}><Trash2 size={14} /></Button>
                                </form>
                              )}
                            </div>

                            {!line.patient_package_id && line.billing_context === "standard" && canEditVisitCharges && linePackageOffers.length > 0 && (
                              <details className="mt-3 rounded-lg border border-[var(--accent-border)] bg-white p-3">
                                <summary className="cursor-pointer text-xs font-black text-[var(--accent-strong)]">تحويل الخدمة دي لباكيدج</summary>
                                <form action={purchasePackageForAdditionalService} className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-end">
                                  <input type="hidden" name="appointment_id" value={appointment.id} />
                                  <input type="hidden" name="patient_id" value={appointment.patient_id} />
                                  <input type="hidden" name="line_id" value={line.id} />
                                  <label className="min-w-0 flex-1 text-xs font-bold">
                                    الباكيدج
                                    <select name="offer_id" required defaultValue="" className="form-control mt-1.5 h-10 min-h-10">
                                      <option value="" disabled>اختار الباكيدج</option>
                                      {linePackageOffers.map((offer) => (
                                        <option key={offer.id} value={offer.id}>
                                          {offer.sessions_count} جلسات · {offer.device_name} · {formatMoney(offer.price_minor, offer.currency)}
                                        </option>
                                      ))}
                                    </select>
                                  </label>
                                  <Button size="sm"><PackagePlus size={14} /> إضافة الباكيدج للحساب</Button>
                                </form>
                              </details>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {canEditVisitCharges ? (
                    <AdditionalServiceForm
                      appointmentId={appointment.id}
                      patientId={appointment.patient_id}
                      primaryServiceId={appointment.service_id}
                      services={services}
                      devicePrices={devicePrices}
                      pulseBalances={pulseBalances}
                      pulsePackOffers={pulsePackOffers}
                      pulseSettings={pulseSettings}
                    />
                  ) : (
                    <div className="text-xs text-[var(--muted)]">لا يمكن تعديل خدمات زيارة ملغاة أو عدم حضور أو موعد تم تغييره.</div>
                  )}
                </div>
              </details>

              {!prepaidBacked && compatiblePackageOffers.length > 0 && canEditVisitCharges && (
                <details className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-3">
                  <summary className="flex cursor-pointer items-center gap-2 text-sm font-black text-slate-900"><PackagePlus size={16} /> تحويل الجلسة الحالية إلى باكيدج</summary>
                  <form action={purchasePackageFromAppointment} className="mt-4 grid gap-3">
                    <input type="hidden" name="appointment_id" value={appointment.id} />
                    <input type="hidden" name="patient_id" value={appointment.patient_id} />
                    <label className="text-xs font-bold">
                      الباكيدج
                      <select name="offer_id" required defaultValue="" className="form-control mt-1.5 h-10 min-h-10">
                        <option value="" disabled>اختار الباكيدج</option>
                        {compatiblePackageOffers.map((offer) => (
                          <option key={offer.id} value={offer.id}>
                            {offer.sessions_count} جلسات · {offer.device_name} · {formatMoney(offer.price_minor, offer.currency)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="rounded-xl border border-[var(--accent-border)] bg-white p-3 text-xs leading-5 text-slate-900">
                      عند التأكيد هيتضاف سعر الباكيدج إلى إجمالي الزيارة، والجلسة الحالية هتتحسب تلقائيًا كأول جلسة منها بدل سعر الجلسة الفردية. الدفع نفسه بيتسجل من قسم المدفوعات تحت.
                    </div>
                    <div><Button><PackagePlus size={15} /> إضافة الباكيدج للحساب واحتساب الجلسة</Button></div>
                  </form>
                </details>
              )}

              {overpaidMinor > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
                  المدفوع المسجل أعلى من الإجمالي الحالي بمقدار {formatMoney(overpaidMinor, payments.currency)}. راجع الاسترداد المناسب بدل حذف أي دفعة قديمة.
                </div>
              )}

              <details className="rounded-xl border border-slate-200 p-3" open={productLines.length > 0 ? true : undefined}>
                <summary className="flex cursor-pointer items-center gap-2 text-sm font-black text-slate-900"><PackagePlus size={16} /> إضافة منتج</summary>
                <div className="mt-4 space-y-3">
                  {productLines.length > 0 && (
                    <div className="space-y-2">
                      {productLines.map((line) => (
                        <div key={line.id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-slate-50 p-3">
                          <div><div className="text-sm font-black">{line.product_name}</div><div className="mt-1 text-xs text-[var(--muted)]">{line.quantity} × {formatMoney(line.unit_price_minor, line.currency)}</div></div>
                          <div className="flex items-center gap-2"><b>{formatMoney(line.total_minor, line.currency)}</b><form action={removeAppointmentProduct}><input type="hidden" name="appointment_id" value={appointment.id} /><input type="hidden" name="patient_id" value={appointment.patient_id} /><input type="hidden" name="line_id" value={line.id} /><Button size="sm" variant="ghost" aria-label={`حذف ${line.product_name}`}><Trash2 size={14} /></Button></form></div>
                        </div>
                      ))}
                    </div>
                  )}
                  {canAddProducts && products.length > 0 ? (
                    <form action={addAppointmentProduct} className="grid gap-3 md:grid-cols-[minmax(180px,1fr)_110px_160px_auto] md:items-end">
                      <input type="hidden" name="appointment_id" value={appointment.id} />
                      <input type="hidden" name="patient_id" value={appointment.patient_id} />
                      <label className="text-xs font-bold">المنتج<select name="product_id" required defaultValue="" className="form-control mt-1.5 h-10 min-h-10"><option value="" disabled>اختار المنتج</option>{products.filter((item) => item.is_active).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
                      <label className="text-xs font-bold">الكمية<input name="quantity" type="number" min="1" max="100" defaultValue="1" required className="form-control mt-1.5 h-10 min-h-10" /></label>
                      <label className="text-xs font-bold">سعر القطعة<input name="unit_price" inputMode="decimal" required placeholder="0.00" className="form-control mt-1.5 h-10 min-h-10" /></label>
                      <Button><PackagePlus size={14} /> إضافة</Button>
                    </form>
                  ) : canAddProducts ? <div className="text-xs text-[var(--muted)]">أضف منتجات العيادة من صفحة المخزن أولًا.</div> : null}
                </div>
              </details>

              {payments.billing_context === "package_prepaid" && (
                <div className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-3 text-sm text-[var(--accent-strong)]">
                  الجلسة الأساسية محسوبة من الباكيدج، وسعر الباكيدج المشتراة من الزيارة ظاهر ضمن الإجمالي المستحق أعلاه.
                </div>
              )}
              {payments.billing_context === "pulse_prepaid" && (
                <div className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-3 text-sm text-[var(--accent-strong)]">
                  سعر الجلسة الأساسية غير محسوب مرة ثانية لأنها من رصيد الـPulses. أي باقة Pulses جديدة أو Pulses إضافية تظهر كبند مستقل في الحساب.
                </div>
              )}
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">صافي المدفوع</div><b className="mt-1 block">{formatMoney(payments.net_paid_minor, payments.currency)}</b></div>
                <div className="rounded-xl bg-[var(--surface-2)] p-3"><div className="text-xs text-[var(--muted)]">المتبقي</div><b className="mt-1 block">{formatMoney(payments.balance_minor, payments.currency)}</b></div>
              </div>

              {payments.refunded_minor > 0 && <div className="text-xs text-[var(--muted)]">تم استرداد {formatMoney(payments.refunded_minor, payments.currency)} من المدفوعات المسجلة.</div>}

              {(payments.balance_minor > 0 || pulseSettlementPending) && ["pending", "confirmed", "completed"].includes(appointment.status) && (
                <AppointmentPaymentForm
                  appointmentId={appointment.id}
                  patientId={appointment.patient_id}
                  currency={payments.currency}
                  subtotalMinor={payments.subtotal_minor ?? payments.price_minor}
                  servicePriceMinor={payments.service_price_minor ?? appointment.price_minor}
                  discountMinor={payments.discount_minor ?? 0}
                  netPaidMinor={payments.net_paid_minor}
                  balanceMinor={payments.balance_minor}
                  pulseCheckout={
                    laserAppointment.laser_device_key &&
                    (laserAppointment.laser_pulses_used ?? 0) > 0 &&
                    !packageBacked &&
                    (!pulseBacked || pulseSettlementPending)
                      ? {
                          lockedToPulse: pulseBacked,
                          deviceName: laserAppointment.laser_device_name || laserAppointment.laser_device_key,
                          pulsesUsed: laserAppointment.laser_pulses_used ?? 0,
                          availableBalance:
                            pulseBacked && pulseSettlement
                              ? pulseSettlement.available_balance_after
                              : patientPulseBalance,
                          deficitPulses: checkoutPulseDeficit,
                          overagePriceMinor: pulseDeviceSetting?.overage_price_minor ?? null,
                          overageCurrency: pulseDeviceSetting?.currency ?? payments.currency,
                          offers: compatiblePulsePackOffers,
                          canSwitchToPulse: pulseBacked || payments.net_paid_minor === 0,
                        }
                      : null
                  }
                />
              )}

              {payments.transactions.length > 0 && (
                <details className="rounded-xl border border-[var(--border)] p-3">
                  <summary className="cursor-pointer text-sm font-bold text-slate-800">سجل المدفوعات والاستردادات</summary>
                  <div className="mt-4 space-y-3">
                    {payments.transactions.slice().reverse().map((transaction) => (
                      <div key={transaction.id} className="rounded-xl bg-[var(--surface-2)] p-3">
                        <div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><ReceiptText size={15} /><b>{transaction.transaction_type === "refund" ? "استرداد" : "دفعة"}</b><Badge tone={transaction.transaction_type === "refund" ? "yellow" : "green"}>{formatMoney(transaction.allocated_amount_minor ?? transaction.amount_minor, transaction.currency)}</Badge></div><span className="text-xs text-[var(--muted)]">{formatDateTime(transaction.created_at)}</span></div>
                        <div className="mt-2 text-xs text-[var(--muted)]">{paymentMethodLabels[transaction.payment_method] || "غير محدد"}{transaction.external_reference ? ` · مرجع ${transaction.external_reference}` : ""}</div>
                        {transaction.reason && <div className="mt-2 text-sm">{transaction.reason}</div>}
                        {payments.can_refund && transaction.transaction_type === "payment" && transaction.refundable_minor > 0 && (
                          <details className="mt-3 rounded-lg border border-red-100 bg-white p-3"><summary className="cursor-pointer text-xs font-bold text-red-700">استرداد من هذه الدفعة</summary><form action={refundAppointmentPayment} className="mt-3 grid gap-2 sm:grid-cols-[140px_1fr_auto]"><input type="hidden" name="appointment_id" value={appointment.id} /><input type="hidden" name="patient_id" value={appointment.patient_id} /><input type="hidden" name="payment_transaction_id" value={transaction.id} /><input name="amount" inputMode="decimal" required defaultValue={minorInput(transaction.refundable_minor)} className="form-control h-9 min-h-9 px-2 text-sm" aria-label="قيمة الاسترداد" /><input name="reason" required maxLength={500} placeholder="سبب الاسترداد" className="form-control h-9 min-h-9 px-2 text-sm" /><Button variant="danger" size="sm">استرداد</Button></form></details>
                        )}
                      </div>
                    ))}
                  </div>
                </details>
              )}

              {!payments.transactions.length && <div className="text-sm text-[var(--muted)]">لا توجد دفعات مسجلة لهذا الموعد حتى الآن.</div>}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-5">
          <Card>
            <CardHeader><CardTitle>متابعة الموعد</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              <details className="rounded-xl border border-[var(--border)] p-3">
                <summary className="flex cursor-pointer items-center gap-2 text-sm font-bold text-slate-800"><Workflow size={15} /> الرسائل التلقائية</summary>
                <div className="mt-3 space-y-3">
                  {detail.automations.map((job) => <div key={job.id} className="rounded-xl bg-[var(--surface-2)] p-3"><div className="flex items-center justify-between gap-2"><b className="text-sm">رسالة تلقائية</b><Badge tone={toneForStatus(job.status)}>{labelForStatus(job.status)}</Badge></div><div className="mt-1 text-xs text-[var(--muted)]">{formatDateTime(job.scheduled_for)}</div>{job.last_error && <div className="mt-2 text-xs font-semibold text-red-700">لم تكتمل الرسالة تلقائيًا. يمكن مراجعتها من صفحة Automation.</div>}</div>)}
                  {!detail.automations.length && <div className="text-sm text-[var(--muted)]">لا توجد رسائل تلقائية مرتبطة بهذا الموعد.</div>}
                </div>
              </details>

              <details className="rounded-xl border border-[var(--border)] p-3">
                <summary className="flex cursor-pointer items-center gap-2 text-sm font-bold text-slate-800"><History size={15} /> سجل تغييرات الحالة</summary>
                <div className="mt-3 space-y-3">
                  {detail.history.map((item) => <div key={item.id} className="border-r-2 border-[var(--accent-border)] pr-3"><div className="flex flex-wrap items-center gap-2"><Badge tone={toneForStatus(item.to_status)}>{appointmentLabels[item.to_status] || "تم تحديث الحالة"}</Badge>{item.from_status && <span className="text-xs text-[var(--muted)]">بعد {appointmentLabels[item.from_status] || "الحالة السابقة"}</span>}</div><div className="mt-1 text-xs text-[var(--muted)]">{formatDateTime(item.created_at)}</div>{item.reason && <div className="mt-1 text-sm">{item.reason}</div>}</div>)}
                </div>
              </details>
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
