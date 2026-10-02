import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { PulseBillingSettings, PulsePackOffer } from "@/lib/types";

import {
  savePulsePackOfferFormAction,
  savePulsePriceFormAction,
} from "./actions";

function NumberField({
  label,
  name,
  defaultValue,
}: {
  label: string;
  name: string;
  defaultValue?: string | number | null;
}) {
  return (
    <label className="grid gap-1.5 text-sm font-medium">
      <span>{label}</span>
      <Input name={name} type="number" min="0" step="1" required defaultValue={defaultValue ?? ""} />
    </label>
  );
}

export function PulsePricingPanel({
  settings,
  offers,
}: {
  settings: PulseBillingSettings[];
  offers: PulsePackOffer[];
}) {
  return (
    <Card id="pulse-pricing">
      <CardHeader>
        <CardTitle>تسعير الـPulses</CardTitle>
        <CardDescription>
          سعر الـPulse الإضافية مستقل لكل جهاز. الباقات كمان مرتبطة بالجهاز ومابتتستخدمش على جهاز مختلف.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid gap-3 md:grid-cols-2">
          {settings.map((setting) => (
            <form
              key={setting.device_key}
              action={savePulsePriceFormAction}
              className="grid items-end gap-3 rounded-xl border border-[var(--border)] p-4"
            >
              <input type="hidden" name="device_key" value={setting.device_key} />
              <div className="font-black text-slate-900">{setting.device_name}</div>
              <label className="grid gap-1.5 text-sm font-medium">
                <span>سعر الـPulse الإضافية</span>
                <Input
                  name="overage_price"
                  type="number"
                  min="0.01"
                  step="0.01"
                  required
                  defaultValue={
                    setting.overage_price_minor === null
                      ? ""
                      : (setting.overage_price_minor / 100).toFixed(2)
                  }
                  placeholder="مثال: 3.00"
                />
                <span className="text-xs font-normal text-[var(--muted)]">
                  بالجنيه المصري لكل Pulse غير مغطاة من رصيد {setting.device_name}.
                </span>
              </label>
              <div>
                <Button type="submit" size="sm">حفظ سعر {setting.device_name}</Button>
              </div>
            </form>
          ))}
        </div>

        <div>
          <div className="mb-2 text-sm font-black">باقات الـPulses</div>
          {offers.length ? (
            <div className="grid gap-2 md:grid-cols-2">
              {offers.map((offer) => (
                <form key={offer.id} action={savePulsePackOfferFormAction} className="rounded-xl bg-[var(--surface-2)] p-3">
                  <input type="hidden" name="device_key" value={offer.device_key} />
                  <input type="hidden" name="pulses_count" value={offer.pulses_count} />
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="font-bold">{offer.pulses_count.toLocaleString("ar-EG")} Pulse · {offer.device_name}</div>
                      <div className="mt-1">
                        <Badge tone={offer.is_active ? "green" : "gray"}>{offer.is_active ? "متاحة" : "متوقفة"}</Badge>
                      </div>
                    </div>
                    <div className="flex flex-wrap items-end gap-2">
                      <label className="grid gap-1 text-xs font-bold">
                        السعر (EGP)
                        <Input
                          name="price"
                          type="number"
                          min="0"
                          step="0.01"
                          required
                          defaultValue={(offer.price_minor / 100).toFixed(2)}
                          className="w-32"
                        />
                      </label>
                      <label className="mb-2 flex items-center gap-2 text-xs font-bold">
                        <input type="checkbox" name="is_active" defaultChecked={offer.is_active} />
                        متاحة للبيع
                      </label>
                      <Button type="submit" size="sm" variant="outline">حفظ</Button>
                    </div>
                  </div>
                </form>
              ))}
            </div>
          ) : (
            <div className="rounded-xl bg-slate-50 p-3 text-sm text-[var(--muted)]">لسه مفيش باقات Pulses مضافة.</div>
          )}
        </div>

        <form action={savePulsePackOfferFormAction} className="grid gap-3 rounded-xl border border-dashed border-slate-300 p-4 md:grid-cols-4 md:items-end">
          <label className="grid gap-1.5 text-sm font-medium">
            <span>الجهاز</span>
            <select
              name="device_key"
              className="form-control h-10 min-h-10"
              defaultValue={settings[0]?.device_key ?? ""}
              disabled={!settings.length}
            >
              {!settings.length ? <option value="">لا توجد أجهزة مفعلة</option> : null}
              {settings.map((setting) => (
                <option key={setting.device_key} value={setting.device_key}>
                  {setting.device_name}
                </option>
              ))}
            </select>
          </label>
          <NumberField label="عدد الـPulses" name="pulses_count" defaultValue={1000} />
          <label className="grid gap-1.5 text-sm font-medium">
            <span>سعر الباقة (EGP)</span>
            <Input name="price" type="number" min="0" step="0.01" required />
          </label>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm font-bold">
              <input type="checkbox" name="is_active" defaultChecked />
              متاحة للبيع
            </label>
            <Button type="submit">حفظ الباقة</Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
