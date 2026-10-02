"use server";

import { randomUUID } from "crypto";
import { revalidatePath } from "next/cache";

import { tiaRequest } from "@/lib/tia/api";

function moneyMinor(value: FormDataEntryValue | null) {
  const amount = Number(String(value || "0"));
  if (!Number.isFinite(amount) || amount < 0) throw new Error("اكتب سعر صحيح.");
  return Math.round(amount * 100);
}

function pulseMoneyMinor(value: FormDataEntryValue | null) {
  const normalized = String(value ?? "").trim().replace(",", ".");
  if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) {
    throw new Error("اكتب سعر صحيح بحد أقصى رقمين عشريين.");
  }
  const [whole, fraction = ""] = normalized.split(".");
  return Number(whole) * 100 + Number((fraction + "00").slice(0, 2));
}

function serviceCategory(formData: FormData, requiresLaserDevice: boolean) {
  if (requiresLaserDevice) return "laser";
  const value = String(formData.get("operational_category") || "");
  if (!["laser", "dermatology", "slimming"].includes(value)) {
    throw new Error("اختار تصنيف الخدمة.");
  }
  return value;
}

function positiveInteger(value: FormDataEntryValue | null, fallback: number) {
  const parsed = Number(String(value || fallback));
  if (!Number.isInteger(parsed) || parsed <= 0) throw new Error("اكتب مدة صحيحة.");
  return parsed;
}

function deviceKeys(formData: FormData) {
  return [...new Set(
    formData
      .getAll("device_key")
      .map((value) => String(value || "").trim())
      .filter(Boolean),
  )];
}

function selectedDeviceKeys(formData: FormData) {
  return deviceKeys(formData).filter(
    (deviceKey) => formData.get(`device_enabled_${deviceKey}`) === "1",
  );
}

function devicePricePayload(formData: FormData, deviceKey: string) {
  return {
    device_key: deviceKey,
    price_minor: moneyMinor(formData.get(`device_price_${deviceKey}`)),
    duration_minutes: positiveInteger(
      formData.get(`device_duration_${deviceKey}`),
      60,
    ),
    currency: "EGP",
  };
}

export async function createService(formData: FormData) {
  const name = String(formData.get("name") || "").trim();
  if (!name) return;

  const requiresLaserDevice = formData.get("requires_laser_device") === "1";
  const selectedDevices = selectedDeviceKeys(formData);
  if (requiresLaserDevice && selectedDevices.length === 0) {
    throw new Error("اختار جهاز واحد على الأقل للخدمة الليزر.");
  }

  const baseDurationMinutes = requiresLaserDevice
    ? positiveInteger(
        formData.get(`device_duration_${selectedDevices[0]}`),
        60,
      )
    : positiveInteger(formData.get("duration_minutes"), 60);

  const service = await tiaRequest<{ id: string }>("/clinic/services", {
    method: "POST",
    body: JSON.stringify({
      name,
      slug: `service-${randomUUID()}`,
      operational_category: serviceCategory(formData, requiresLaserDevice),
      description: null,
      duration_minutes: baseDurationMinutes,
      buffer_before_minutes: 0,
      buffer_after_minutes: 0,
      price_minor: requiresLaserDevice ? 0 : moneyMinor(formData.get("price")),
      currency: "EGP",
      requires_medical_review: false,
      requires_laser_device: requiresLaserDevice,
    }),
  });

  if (requiresLaserDevice) {
    await Promise.all(
      selectedDevices.map((deviceKey) =>
        tiaRequest("/inventory/laser-prices", {
          method: "PUT",
          body: JSON.stringify({
            service_id: service.id,
            ...devicePricePayload(formData, deviceKey),
          }),
        }),
      ),
    );
  }

  revalidatePath("/services");
  revalidatePath("/appointments");
}

export async function updateServicePricing(formData: FormData) {
  const serviceId = String(formData.get("service_id") || "");
  const name = String(formData.get("name") || "").trim();
  const requiresLaserDevice = formData.get("requires_laser_device") === "1";
  const category = serviceCategory(formData, requiresLaserDevice);
  if (!serviceId || !name) return;

  const allDevices = deviceKeys(formData);
  const selectedDevices = selectedDeviceKeys(formData);

  if (requiresLaserDevice) {
    if (selectedDevices.length === 0) {
      throw new Error("اختار جهاز واحد على الأقل للخدمة الليزر.");
    }
    const primaryDuration = positiveInteger(
      formData.get(`device_duration_${selectedDevices[0]}`),
      60,
    );

    await tiaRequest(`/clinic/services/${serviceId}`, {
      method: "PATCH",
      body: JSON.stringify({
        name,
        operational_category: category,
        price_minor: 0,
        duration_minutes: primaryDuration,
        requires_laser_device: true,
      }),
    });

    await Promise.all(
      allDevices.map((deviceKey) =>
        selectedDevices.includes(deviceKey)
          ? tiaRequest("/inventory/laser-prices", {
              method: "PUT",
              body: JSON.stringify({
                service_id: serviceId,
                ...devicePricePayload(formData, deviceKey),
              }),
            })
          : tiaRequest(
              `/inventory/laser-prices/${serviceId}/${encodeURIComponent(deviceKey)}`,
              { method: "DELETE" },
            ),
      ),
    );
  } else {
    await tiaRequest(`/clinic/services/${serviceId}`, {
      method: "PATCH",
      body: JSON.stringify({
        name,
        operational_category: category,
        price_minor: moneyMinor(formData.get("price")),
        duration_minutes: positiveInteger(formData.get("duration_minutes"), 60),
        requires_laser_device: false,
      }),
    });
    await Promise.all(
      allDevices.map((deviceKey) =>
        tiaRequest(
          `/inventory/laser-prices/${serviceId}/${encodeURIComponent(deviceKey)}`,
          { method: "DELETE" },
        ),
      ),
    );
  }

  revalidatePath("/services");
  revalidatePath("/appointments");
}

export async function updateLaserDevicePrice(formData: FormData) {
  const serviceId = String(formData.get("service_id") || "");
  const deviceKey = String(formData.get("device_key") || "").trim();
  if (!serviceId || !deviceKey) return;

  await tiaRequest("/inventory/laser-prices", {
    method: "PUT",
    body: JSON.stringify({
      service_id: serviceId,
      device_key: deviceKey,
      price_minor: moneyMinor(formData.get("price")),
      duration_minutes: positiveInteger(formData.get("duration_minutes"), 60),
      currency: "EGP",
    }),
  });
  revalidatePath("/services");
  revalidatePath("/appointments");
}

export async function updatePackageOffer(formData: FormData) {
  const serviceId = String(formData.get("service_id") || "");
  const deviceKey = String(formData.get("device_key") || "").trim() || null;
  const sessionsCount = Number(String(formData.get("sessions_count") || "0"));
  if (!serviceId) return;
  if (!Number.isInteger(sessionsCount) || sessionsCount <= 0) {
    throw new Error("اكتب عدد جلسات صحيح أكبر من صفر.");
  }

  await tiaRequest("/booking/package-offers", {
    method: "PUT",
    body: JSON.stringify({
      service_id: serviceId,
      device_key: deviceKey,
      sessions_count: sessionsCount,
      price_minor: moneyMinor(formData.get("price")),
      currency: "EGP",
      is_active: formData.get("is_active") === "1",
    }),
  });
  revalidatePath("/services");
}

export async function savePulsePriceFormAction(formData: FormData) {
  const deviceKey = String(formData.get("device_key") || "").trim();
  if (!deviceKey) throw new Error("اختار جهاز ليزر.");

  await tiaRequest("/booking/pulse-device-prices", {
    method: "PUT",
    body: JSON.stringify({
      device_key: deviceKey,
      overage_price_minor: pulseMoneyMinor(formData.get("overage_price")),
      currency: "EGP",
    }),
  });
  revalidatePath("/services");
  revalidatePath("/appointments");
  revalidatePath("/finance");
}

export async function savePulsePackOfferFormAction(formData: FormData) {
  const deviceKey = String(formData.get("device_key") || "").trim();
  const pulsesCount = Number(String(formData.get("pulses_count") || "0"));
  if (!deviceKey) throw new Error("اختار جهاز ليزر.");
  if (!Number.isInteger(pulsesCount) || pulsesCount <= 0) {
    throw new Error("عدد الـPulses لازم يكون رقم صحيح أكبر من صفر.");
  }

  await tiaRequest("/booking/pulse-pack-offers", {
    method: "PUT",
    body: JSON.stringify({
      device_key: deviceKey,
      pulses_count: pulsesCount,
      price_minor: pulseMoneyMinor(formData.get("price")),
      currency: "EGP",
      is_active: formData.get("is_active") === "on",
    }),
  });
  revalidatePath("/services");
  revalidatePath("/patients");
  revalidatePath("/appointments");
}
