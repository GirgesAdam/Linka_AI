"use client";

import { useState } from "react";

import { Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice } from "@/lib/types";

import { updateServicePricing } from "./actions";

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

export function ServicePricingForm({
  service,
  devicePrices,
  devices,
}: {
  service: ServiceConfig;
  devicePrices: DeviceConfig[];
  devices: ClinicLaserDevice[];
}) {
  const [requiresLaserDevice, setRequiresLaserDevice] = useState(service.requires_laser_device);
  const [category, setCategory] = useState(service.operational_category);
  const activeDevices = devices.filter((device) => device.is_active);
  const config = (key: string) => devicePrices.find((item) => item.device_key === key);

  return (
    <form action={updateServicePricing} className="grid gap-3 rounded-xl bg-slate-50 p-3">
      <input type="hidden" name="service_id" value={service.id} />
      {activeDevices.map((device) => (
        <input key={device.id} type="hidden" name="device_key" value={device.device_key} />
      ))}
      <div className="grid gap-3 md:grid-cols-[minmax(180px,1.4fr)_minmax(130px,.7fr)_auto_auto] md:items-end">
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">اسم الخدمة</span>
          <Input name="name" required maxLength={200} defaultValue={service.name} />
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">التصنيف</span>
          {requiresLaserDevice && <input type="hidden" name="operational_category" value="laser" />}
          <select
            name="operational_category"
            value={requiresLaserDevice ? "laser" : category}
            disabled={requiresLaserDevice}
            onChange={(event) => setCategory(event.target.value as typeof category)}
            className="form-control h-10 min-h-10"
          >
            <option value="laser">ليزر</option>
            <option value="dermatology">جلدية</option>
            <option value="slimming">تخسيس</option>
          </select>
        </label>
        <label className="flex h-10 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-xs font-bold text-slate-700">
          <input
            type="checkbox"
            name="requires_laser_device"
            value="1"
            checked={requiresLaserDevice}
            onChange={(event) => {
              setRequiresLaserDevice(event.target.checked);
              if (event.target.checked) setCategory("laser");
            }}
          />
          خدمة تحتاج اختيار جهاز ليزر
        </label>
        <Button type="submit" size="sm"><Save size={14} /> حفظ</Button>
      </div>

      {requiresLaserDevice ? (
        <div className="grid gap-3">
          {activeDevices.length ? (
            <>
              <p className="text-xs text-[var(--muted)]">
                فعّل فقط الأجهزة التي تُستخدم في هذه الخدمة. تعطيل الجهاز هنا لا يحذفه من العيادة ولا من السجلات القديمة.
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
            <Input name="price" type="number" min="0" step="0.01" required defaultValue={major(service.price_minor)} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-bold text-slate-600">المدة بالدقائق</span>
            <Input name="duration_minutes" type="number" min="1" max="1440" required defaultValue={service.duration_minutes} />
          </label>
        </div>
      )}
    </form>
  );
}
