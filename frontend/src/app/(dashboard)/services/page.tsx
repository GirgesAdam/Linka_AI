import { ChevronDown, Cpu, PackageCheck, PackagePlus, Save, Settings2 } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatMoney } from "@/lib/format";
import type { ClinicLaserDevice, PulseBillingSettings, PulsePackOffer } from "@/lib/types";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";

import { updatePackageOffer } from "./actions";
import { PulsePricingPanel } from "./pulse-pricing-panel";
import { ServiceCreateForm } from "./service-create-form";
import { ServicePricingForm } from "./service-pricing-form";

type Service = {
  id: string;
  name: string;
  category: string | null;
  operational_category: "laser" | "dermatology" | "slimming";
  duration_minutes: number;
  price_minor: number;
  currency: string;
  requires_laser_device: boolean;
  is_active: boolean;
};
type DevicePrice = {
  service_id: string;
  device_key: string;
  device_name: string;
  price_minor: number | null;
  duration_minutes: number | null;
  currency: string;
  configured: boolean;
};
type PackageOffer = {
  id: string;
  service_id: string;
  service_name: string;
  device_key: string | null;
  device_name: string | null;
  sessions_count: number;
  price_minor: number;
  currency: string;
  is_active: boolean;
  standalone_session_price_minor: number;
  savings_minor: number;
};

function categoryLabel(value: Service["operational_category"]) {
  return value === "laser" ? "ليزر" : value === "slimming" ? "تخسيس" : "جلدية";
}

function major(minor: number | null) {
  return minor == null ? "" : String(minor / 100);
}

function PackageOffersEditor({
  serviceId,
  deviceKey,
  offers,
  isAdmin,
  enabled = true,
  disabledMessage,
}: {
  serviceId: string;
  deviceKey: string | null;
  offers: PackageOffer[];
  isAdmin: boolean;
  enabled?: boolean;
  disabledMessage?: string;
}) {
  if (!enabled) {
    return (
      <div className="rounded-xl bg-amber-50 p-3 text-xs font-semibold text-amber-900">
        {disabledMessage || "حدد سعر الخدمة أولًا قبل تفعيل باكيدجاتها."}
      </div>
    );
  }

  const sortedOffers = [...offers].sort((left, right) => left.sessions_count - right.sessions_count);
  const activeOffers = sortedOffers.filter((offer) => offer.is_active);

  if (!isAdmin) {
    return activeOffers.length ? (
      <div className="space-y-2">
        {activeOffers.map((offer) => (
          <div key={offer.id} className="flex items-center justify-between rounded-xl bg-slate-50 px-3 py-2 text-sm">
            <b>{offer.sessions_count} جلسات</b>
            <span>{formatMoney(offer.price_minor, offer.currency)}</span>
          </div>
        ))}
      </div>
    ) : (
      <div className="rounded-xl bg-slate-50 p-3 text-xs font-semibold text-slate-500">
        لا توجد باكيدجات مفعلة حاليًا.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {sortedOffers.map((offer) => (
        <form
          key={offer.id}
          action={updatePackageOffer}
          className="grid gap-2 rounded-xl bg-slate-50 p-3 sm:grid-cols-[110px_minmax(120px,1fr)_auto]"
        >
          <input type="hidden" name="service_id" value={serviceId} />
          {deviceKey ? <input type="hidden" name="device_key" value={deviceKey} /> : null}
          <input type="hidden" name="sessions_count" value={offer.sessions_count} />
          <label className="flex h-10 items-center gap-2 text-xs font-black">
            <input type="checkbox" name="is_active" value="1" defaultChecked={offer.is_active} />
            {offer.sessions_count} جلسات
          </label>
          <label>
            <span className="mb-1 block text-[11px] font-bold text-slate-600">سعر الباكيدج</span>
            <Input
              name="price"
              type="number"
              min="0"
              step="0.01"
              required
              defaultValue={major(offer.price_minor)}
            />
          </label>
          <Button type="submit" size="sm" variant="outline"><Save size={13} /> حفظ</Button>
          {offer.is_active && (
            <div className="text-[11px] font-semibold text-slate-500 sm:col-span-3">
              {offer.savings_minor > 0
                ? `توفير ${formatMoney(offer.savings_minor, offer.currency)} مقارنة بـ ${offer.sessions_count} جلسات منفصلة.`
                : "لا يوجد خصم مقارنة بسعر الجلسات المنفصلة."}
            </div>
          )}
        </form>
      ))}

      <form
        action={updatePackageOffer}
        className="grid gap-2 rounded-xl border border-dashed border-slate-300 bg-white p-3 sm:grid-cols-[110px_minmax(120px,1fr)_auto_auto]"
      >
        <input type="hidden" name="service_id" value={serviceId} />
        {deviceKey ? <input type="hidden" name="device_key" value={deviceKey} /> : null}
        <label>
          <span className="mb-1 block text-[11px] font-bold text-slate-600">عدد الجلسات</span>
          <Input
            name="sessions_count"
            type="number"
            min="1"
            step="1"
            inputMode="numeric"
            required
            placeholder="مثلاً 4"
          />
        </label>
        <label>
          <span className="mb-1 block text-[11px] font-bold text-slate-600">سعر الباكيدج</span>
          <Input name="price" type="number" min="0" step="0.01" required placeholder="السعر الإجمالي" />
        </label>
        <label className="flex h-10 items-center gap-2 text-xs font-black">
          <input type="checkbox" name="is_active" value="1" defaultChecked />
          مفعلة
        </label>
        <Button type="submit" size="sm"><Save size={13} /> إضافة</Button>
      </form>
    </div>
  );
}

export default async function ServicesPage() {
  const [{ workspace }, services, devices, devicePrices, packageOffers, pulseSettings, pulseOffers] = await Promise.all([
    getAppContext(),
    tiaRequest<Service[]>("/clinic/services"),
    tiaRequest<ClinicLaserDevice[]>("/inventory/laser-devices"),
    tiaRequest<DevicePrice[]>("/inventory/laser-prices").catch(() => []),
    tiaRequest<PackageOffer[]>("/booking/package-offers").catch(() => []),
    tiaRequest<PulseBillingSettings[]>("/booking/pulse-device-prices"),
    tiaRequest<PulsePackOffer[]>("/booking/pulse-pack-offers"),
  ]);
  const isAdmin = workspace.role === "admin";
  const byService = new Map<string, DevicePrice[]>();
  for (const price of devicePrices) byService.set(price.service_id, [...(byService.get(price.service_id) || []), price]);
  return (
    <>
      <PageHeader
        title="الخدمات والأسعار"
        description="أضف الخدمات وعدّل أسعارها ومددها وباقاتها. خدمات الليزر لها إعداد مستقل لكل جهاز، ومن هنا تقدر كمان تسعّر الـPulses وباقاتها."
      />
      {isAdmin && (
        <Card className="mb-5">
          <CardHeader><CardTitle className="flex items-center gap-2"><PackagePlus size={18} /> إضافة خدمة</CardTitle></CardHeader>
          <CardContent>
            <ServiceCreateForm devices={devices} />
            <p className="mt-3 text-xs text-[var(--muted)]">لو الخدمة تحتاج جهاز ليزر، السعر والمدة بيتحددوا لكل جهاز بدل السعر والمدة الأساسيين.</p>
          </CardContent>
        </Card>
      )}

      {isAdmin && (
        <div className="mb-5">
          <PulsePricingPanel settings={pulseSettings} offers={pulseOffers} />
        </div>
      )}

      <div className="space-y-4">
        {services.map((service) => {
          const prices = byService.get(service.id) || [];
          const serviceOffers = packageOffers.filter(
            (offer) => offer.service_id === service.id && offer.device_key === null,
          );
          const activeDevices = devices.filter((device) => device.is_active);
          const configuredDeviceCount = activeDevices.filter((device) => {
            const row = prices.find((item) => item.device_key === device.device_key);
            return Boolean(row?.configured && row.price_minor != null);
          }).length;

          return (
            <details
              key={service.id}
              className="group overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm transition-shadow open:shadow-md"
            >
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-4 sm:px-5">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="truncate text-base font-black text-slate-950">{service.name}</h2>
                    <span className={`rounded-full px-2 py-1 text-[11px] font-black ${
                      service.is_active ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"
                    }`}>
                      {service.is_active ? "مفعلة" : "متوقفة"}
                    </span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs font-semibold text-slate-500">
                    <span>{categoryLabel(service.operational_category)}</span>
                    <span>
                      {service.requires_laser_device
                        ? `${configuredDeviceCount} من ${activeDevices.length} أجهزة مفعلة`
                        : `${service.duration_minutes} دقيقة`}
                    </span>
                    <span>
                      {service.requires_laser_device
                        ? "السعر حسب الجهاز"
                        : formatMoney(service.price_minor, service.currency)}
                    </span>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2 text-xs font-black text-slate-600">
                  <span className="hidden sm:inline">عرض التفاصيل</span>
                  <ChevronDown size={18} className="transition-transform group-open:rotate-180" />
                </div>
              </summary>

              <div className="space-y-3 border-t border-slate-100 bg-slate-50/40 p-3 sm:p-4">
                {isAdmin ? (
                  <details className="group/section rounded-xl border border-slate-200 bg-white">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3">
                      <span className="flex items-center gap-2 text-sm font-black text-slate-900">
                        <Settings2 size={16} />
                        إعدادات الخدمة والسعر
                      </span>
                      <ChevronDown size={16} className="transition-transform group-open/section:rotate-180" />
                    </summary>
                    <div className="border-t border-slate-100 p-3">
                      <ServicePricingForm service={service} devicePrices={prices} devices={devices} />
                    </div>
                  </details>
                ) : null}

                {service.requires_laser_device ? (
                  <div className="space-y-2">
                    {activeDevices.map((device) => {
                      const row = prices.find((item) => item.device_key === device.device_key);
                      const configured = Boolean(row?.configured && row.price_minor != null);
                      const deviceOffers = packageOffers
                        .filter(
                          (offer) =>
                            offer.service_id === service.id &&
                            offer.device_key === device.device_key,
                        )
                        .sort((left, right) => left.sessions_count - right.sessions_count);
                      const activeOfferCount = deviceOffers.filter((offer) => offer.is_active).length;

                      return (
                        <details key={device.id} className="group/device rounded-xl border border-slate-200 bg-white">
                          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3">
                            <div className="min-w-0">
                              <div className="flex items-center gap-2 font-black text-slate-900">
                                <Cpu size={16} />
                                <span className="truncate">{device.name}</span>
                              </div>
                              <div className="mt-1 flex flex-wrap gap-2 text-[11px] font-semibold text-slate-500">
                                {configured && row?.price_minor != null ? (
                                  <>
                                    <span>{formatMoney(row.price_minor, row.currency)}</span>
                                    <span>·</span>
                                    <span>{row.duration_minutes ?? service.duration_minutes} دقيقة</span>
                                    <span>·</span>
                                    <span>{activeOfferCount} باكيدج مفعلة</span>
                                  </>
                                ) : (
                                  <span>غير مفعّل على الخدمة</span>
                                )}
                              </div>
                            </div>
                            <ChevronDown size={16} className="shrink-0 transition-transform group-open/device:rotate-180" />
                          </summary>
                          <div className="border-t border-slate-100 p-4">
                            <div className="mb-3 flex items-center gap-2 text-sm font-black text-slate-900">
                              <PackageCheck size={16} />
                              الباكيدجات
                            </div>
                            <PackageOffersEditor
                              serviceId={service.id}
                              deviceKey={device.device_key}
                              offers={deviceOffers}
                              isAdmin={isAdmin}
                              enabled={configured}
                              disabledMessage="فعّل الجهاز وحدد سعر الجلسة والمدة أولًا قبل تفعيل باكيدجاته."
                            />
                          </div>
                        </details>
                      );
                    })}
                  </div>
                ) : (
                  <details className="group/packages rounded-xl border border-slate-200 bg-white">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3">
                      <span className="flex items-center gap-2 text-sm font-black text-slate-900">
                        <PackageCheck size={16} />
                        الباكيدجات
                        <span className="text-xs font-semibold text-slate-500">
                          ({serviceOffers.filter((offer) => offer.is_active).length} مفعلة)
                        </span>
                      </span>
                      <ChevronDown size={16} className="transition-transform group-open/packages:rotate-180" />
                    </summary>
                    <div className="border-t border-slate-100 p-4">
                      <PackageOffersEditor
                        serviceId={service.id}
                        deviceKey={null}
                        offers={serviceOffers}
                        isAdmin={isAdmin}
                      />
                    </div>
                  </details>
                )}
              </div>
            </details>
          );
        })}
      </div>
    </>
  );
}
