"use client";

import { useActionState, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice } from "@/lib/types";

import { createService, type ServiceMutationActionState } from "./actions";

const initialServiceMutationActionState: ServiceMutationActionState = {
  ok: false,
  error: null,
  revision: null,
};

export function ServiceCreateForm({ devices }: { devices: ClinicLaserDevice[] }) {
  const [category, setCategory] = useState<"laser" | "dermatology" | "slimming">("dermatology");
  const formRef = useRef<HTMLFormElement>(null);
  const router = useRouter();
  const [state, formAction, pending] = useActionState(
    async (previous: ServiceMutationActionState, formData: FormData) => {
      const next = await createService(previous, formData);
      if (next.ok) {
        formRef.current?.reset();
        setCategory("dermatology");
        router.refresh();
      }
      return next;
    },
    initialServiceMutationActionState,
  );
  const requiresLaserDevice = category === "laser";
  const activeDevices = devices.filter((device) => device.is_active);

  return (
    <form ref={formRef} action={formAction} className="grid gap-3 md:grid-cols-2 xl:grid-cols-6 xl:items-end">
      <label className="xl:col-span-2">
        <span className="mb-1.5 block text-xs font-bold">اسم الخدمة</span>
        <Input name="name" required maxLength={200} placeholder="مثال: Full Body" disabled={pending} />
      </label>
      <label>
        <span className="mb-1.5 block text-xs font-bold">التصنيف</span>
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

      <div className="xl:row-span-2">
        <Button type="submit" className="w-full" disabled={pending}>
          {pending ? "جاري الإضافة…" : "إضافة"}
        </Button>
      </div>

      {state.error && (
        <div className="rounded-lg bg-red-50 p-3 text-sm font-bold text-red-700 md:col-span-2 xl:col-span-6">
          {state.error}
        </div>
      )}

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
                      <input type="checkbox" name={`device_enabled_${device.device_key}`} value="1" disabled={pending} />
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
                        disabled={pending}
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
                        disabled={pending}
                      />
                    </label>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
              أضف جهاز ليزر من إعدادات العيادة أولًا قبل إنشاء خدمة ليزر.
            </div>
          )}
        </div>
      ) : (
        <div className="grid gap-3 md:col-span-2 md:grid-cols-2 xl:col-span-6">
          <label>
            <span className="mb-1.5 block text-xs font-bold">السعر الأساسي</span>
            <Input name="price" type="number" min="0" step="0.01" defaultValue="0" required disabled={pending} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-bold">المدة بالدقائق</span>
            <Input name="duration_minutes" type="number" min="1" max="1440" defaultValue="60" required disabled={pending} />
          </label>
        </div>
      )}
    </form>
  );
}
