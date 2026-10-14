"use client";

import { useActionState, useState } from "react";
import { useRouter } from "next/navigation";

import { Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice } from "@/lib/types";

import { type ServiceMutationActionState, updateServicePricing } from "./actions";

type DeviceConfig = {
  device_key: string;
  device_name: string;
  price_minor: number | null;
  duration_minutes: number | null;
  configured: boolean;
};

type ServiceConfig = {
  id: string;
  name: string;
  duration_minutes: number;
  price_minor: number;
  category: string | null;
  operational_category: "laser" | "dermatology" | "slimming";
  requires_laser_device: boolean;
};

function major(minor: number | null | undefined) {
  return minor == null ? "" : String(minor / 100);
}

const initialServiceMutationActionState: ServiceMutationActionState = {
  ok: false,
  error: null,
  revision: null,
};

export function ServicePricingForm({
  service,
  devicePrices,
  devices,
}: {
  service: ServiceConfig;
  devicePrices: DeviceConfig[];
  devices: ClinicLaserDevice[];
}) {
  const [category, setCategory] = useState(service.operational_category);
  const router = useRouter();
  const [state, formAction, pending] = useActionState(
    async (previous: ServiceMutationActionState, formData: FormData) => {
      const next = await updateServicePricing(previous, formData);
      if (next.ok) router.refresh();
      return next;
    },
    initialServiceMutationActionState,
  );
  const requiresLaserDevice = category === "laser";
  const activeDevices = devices.filter((device) => device.is_active);
  const config = (key: string) => devicePrices.find((item) => item.device_key === key);

  return (
    <form action={formAction} className="grid gap-3 rounded-xl bg-slate-50 p-3">
      <input type="hidden" name="service_id" value={service.id} />
      {activeDevices.map((device) => (
        <input key={device.id} type="hidden" name="device_key" value={device.device_key} />
      ))}
      <div className="grid gap-3 md:grid-cols-[minmax(180px,1.4fr)_minmax(130px,.7fr)_auto] md:items-end">
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">اسم الخدمة</span>
          <Input name="name" required maxLength={200} defaultValue={service.name} disabled={pending} />
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">التصنيف</span>
          <select
            name="operational_category"
            value={category}
            onChange={(event) => setCategory(event.target.value as typeof category)}
            disabled={pending}
            className="form-control h-10 min-h-10"
          >
            <option value="laser">ليزر</option>
            <option value="dermatology">جلدية</option>
            <option value="slimming">تخسيس</option>
          </select>
        </label>
        <Button type="submit" size="sm" disabled={pending}>
          <Save size={14} /> {pending ? "جاري الحفظ…" : "حفظ"}
        </Button>
      </div>

      {state.error && (
        <div className="rounded-lg bg-red-50 p-3 text-sm font-bold text-red-700">{state.error}</div>
      )}

      {requiresLaserDevice ? (
        <div className="grid gap-3">
          {activeDevices.length ? (
            <>
              <p className="text-xs text-[var(--muted)]">
                فعّل فقط الأجهزة التي تُستخدم في هذه الخدمة. تغيير التصنيف لغير ليزر يعطّل ربط الأجهزة دون حذف السجلات القديمة.
              </p>
              <div className="grid gap-3 lg:grid-cols-2">
                {activeDevices.map((device) => {
                  const row = config(device.device_key);
                  return (
                    <div key={device.id} className="grid gap-3 rounded-xl border border-slate-200 bg-white p-3 sm:grid-cols-2">
                      <label className="sm:col-span-2 flex items-center gap-2 text-sm font-black text-slate-900">
                        <input
                          type="checkbox"
                          name={`device_enabled_${device.device_key}`}
                          value="1"
                          defaultChecked={Boolean(row?.configured)}
                          disabled={pending}
                        />
                        {device.name}
                      </label>
                      <label>
                        <span className="mb-1.5 block text-xs font-bold text-slate-600">سعر الجلسة</span>
                        <Input
                          name={`device_price_${device.device_key}`}
                          type="number"
                          min="0"
                          step="0.01"
                          defaultValue={major(row?.price_minor)}
                          placeholder="السعر"
                          disabled={pending}
                        />
                      </label>
                      <label>
                        <span className="mb-1.5 block text-xs font-bold text-slate-600">مدة الجلسة بالدقائق</span>
                        <Input
                          name={`device_duration_${device.device_key}`}
                          type="number"
                          min="1"
                          max="1440"
                          defaultValue={row?.duration_minutes ?? service.duration_minutes}
                          disabled={pending}
                        />
                      </label>
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
              أضف جهاز ليزر من إعدادات العيادة أولًا.
            </div>
          )}
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          <label>
            <span className="mb-1.5 block text-xs font-bold text-slate-600">السعر الأساسي بالجنيه</span>
            <Input name="price" type="number" min="0" step="0.01" required defaultValue={major(service.price_minor)} disabled={pending} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-bold text-slate-600">المدة بالدقائق</span>
            <Input name="duration_minutes" type="number" min="1" max="1440" required defaultValue={service.duration_minutes} disabled={pending} />
          </label>
        </div>
      )}
    </form>
  );
}
