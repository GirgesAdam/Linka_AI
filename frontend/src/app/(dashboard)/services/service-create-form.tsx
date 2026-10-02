"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice } from "@/lib/types";

import { createService } from "./actions";

export function ServiceCreateForm({ devices }: { devices: ClinicLaserDevice[] }) {
  const [requiresLaserDevice, setRequiresLaserDevice] = useState(false);
  const [category, setCategory] = useState<"laser" | "dermatology" | "slimming">("dermatology");
  const activeDevices = devices.filter((device) => device.is_active);

  return (
    <form action={createService} className="grid gap-3 md:grid-cols-2 xl:grid-cols-6 xl:items-end">
      <label className="xl:col-span-2">
        <span className="mb-1.5 block text-xs font-bold">اسم الخدمة</span>
        <Input name="name" required maxLength={200} placeholder="مثال: Full Body" />
      </label>
      <label>
        <span className="mb-1.5 block text-xs font-bold">التصنيف</span>
        {requiresLaserDevice && <input type="hidden" name="operational_category" value="laser" />}
        <select
          name="operational_category"
          value={requiresLaserDevice ? "laser" : category}
          disabled={requiresLaserDevice}
          onChange={(event) => setCategory(event.target.value as "laser" | "dermatology" | "slimming")}
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
        يحتاج تحديد جهاز ليزر
      </label>

      <div className="xl:row-span-2">
        <Button type="submit" className="w-full">إضافة</Button>
      </div>

      {requiresLaserDevice ? (
        <div className="grid gap-3 md:col-span-2 xl:col-span-6">
          {activeDevices.length ? (
            <>
              <p className="text-xs text-[var(--muted)]">
                اختار الأجهزة التي تُستخدم فعليًا لهذه الخدمة وحدد سعر ومدة الجلسة على كل جهاز.
              </p>
              <div className="grid gap-3 lg:grid-cols-2">
                {activeDevices.map((device) => (
                  <div key={device.id} className="grid gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3 sm:grid-cols-2">
                    <input type="hidden" name="device_key" value={device.device_key} />
                    <label className="sm:col-span-2 flex items-center gap-2 text-sm font-black text-slate-900">
                      <input type="checkbox" name={`device_enabled_${device.device_key}`} value="1" />
                      {device.name}
                    </label>
                    <label>
                      <span className="mb-1.5 block text-xs font-bold">سعر الجلسة</span>
                      <Input
                        name={`device_price_${device.device_key}`}
                        type="number"
                        min="0"
                        step="0.01"
                        placeholder="السعر"
                      />
                    </label>
                    <label>
                      <span className="mb-1.5 block text-xs font-bold">مدة الجلسة بالدقائق</span>
                      <Input
                        name={`device_duration_${device.device_key}`}
                        type="number"
                        min="1"
                        max="1440"
                        defaultValue="60"
                      />
                    </label>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
              أضف جهاز ليزر من إعدادات العيادة أولًا قبل إنشاء خدمة تعتمد على جهاز.
            </div>
          )}
        </div>
      ) : (
        <div className="grid gap-3 md:col-span-2 md:grid-cols-2 xl:col-span-6">
          <label>
            <span className="mb-1.5 block text-xs font-bold">السعر الأساسي</span>
            <Input name="price" type="number" min="0" step="0.01" defaultValue="0" required />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-bold">المدة بالدقائق</span>
            <Input name="duration_minutes" type="number" min="1" max="1440" defaultValue="60" required />
          </label>
        </div>
      )}
    </form>
  );
}
