"use server";

import { randomUUID } from "node:crypto";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { TiaApiError, tiaRequest } from "@/lib/tia/api";
import type { Appointment } from "@/lib/types";

function refreshAppointmentViews(appointmentId: string, patientId?: string) {
  revalidatePath("/appointments");
  revalidatePath("/doctors");
  revalidatePath(`/appointments/${appointmentId}`);
  if (patientId) revalidatePath(`/patients/${patientId}`);
  revalidatePath("/dashboard");
  revalidatePath("/finance");
  revalidatePath("/analytics");
}

export async function updateLaserPulses(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const pulsesUsed = Number(String(formData.get("pulses_used") || ""));
  if (!appointmentId || !Number.isInteger(pulsesUsed) || pulsesUsed < 0) return;
  try {
    await tiaRequest(`/booking/appointments/${appointmentId}/laser-usage`, {
      method: "PUT",
      body: JSON.stringify({ pulses_used: pulsesUsed }),
    });
  } catch (error) {
    const message =
      error instanceof TiaApiError
        ? error.message
        : "ØªØ¹Ø°Ø± Ø­ÙØ¸ Ø§Ø³ØªÙ‡Ù„Ø§Ùƒ Ø§Ù„Ù€Pulses. Ø­Ø§ÙˆÙ„ Ù…Ø±Ø© Ø£Ø®Ø±Ù‰.";
    redirect(`/appointments/${appointmentId}?visit_error=${encodeURIComponent(message)}`);
  }
  refreshAppointmentViews(appointmentId, patientId || undefined);
  redirect(`/appointments/${appointmentId}?visit_saved=pulse_usage`);
}

export async function confirmAppointment(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  if (!appointmentId) return;
  await tiaRequest(`/booking/appointments/${appointmentId}/confirm`, { method: "POST" });
  refreshAppointmentViews(appointmentId, patientId || undefined);
}

export async function updateAppointmentStatus(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const status = String(formData.get("status") || "");
  if (!appointmentId || !status) return;
  await tiaRequest(`/booking/appointments/${appointmentId}/status`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });
  refreshAppointmentViews(appointmentId, patientId || undefined);
}

export type AppointmentServiceChangeState = { ok: boolean; error: string | null };

function serviceChangeError(error: unknown) {
  if (!(error instanceof TiaApiError)) return "ØªØ¹Ø°Ø± ØªØ¹Ø¯ÙŠÙ„ Ø§Ù„Ù…ÙˆØ¹Ø¯. Ø­Ø§ÙˆÙ„ Ù…Ø±Ø© Ø£Ø®Ø±Ù‰.";
  const detail = error.technicalMessage || "";
  if (detail.includes("same service category")) {
    return "الميعاد ده غير متاح لأن فيه جلسة تانية في نفس فئة الخدمات خلال نفس الوقت. اختار وقت مختلف.";
  }
  if (
    detail.includes("not available for the selected service/device with the selected doctor") ||
    detail.includes("Requested appointment time is not available")
  ) {
    return "Ø§Ù„Ø®Ø¯Ù…Ø© Ø£Ùˆ Ø§Ù„Ø¬Ù‡Ø§Ø² Ø£Ùˆ Ø§Ù„Ø¯ÙƒØªÙˆØ± Ù…Ø´ Ù…ØªØ§Ø­ÙŠÙ† ÙÙŠ Ø§Ù„ÙˆÙ‚Øª Ø§Ù„Ù…Ø®ØªØ§Ø±. ØºÙŠÙ‘Ø± Ø§Ù„ÙˆÙ‚Øª Ø£Ùˆ Ø§Ø®ØªØ§Ø± Ø¯ÙƒØªÙˆØ± ØªØ§Ù†ÙŠ.";
  }
  if (detail.includes("another appointment") || detail.includes("already booked at this time")) {
    return "ÙÙŠÙ‡ ØªØ¹Ø§Ø±Ø¶ Ù…Ø¹ Ù…ÙˆØ¹Ø¯ ØªØ§Ù†ÙŠ Ù„Ù„Ø¯ÙƒØªÙˆØ± Ø£Ùˆ Ø§Ù„Ø¬Ù‡Ø§Ø² ÙÙŠ Ø§Ù„ÙˆÙ‚Øª Ø¯Ù‡. Ø§Ø®ØªØ§Ø± ÙˆÙ‚Øª Ù…Ø®ØªÙ„Ù.";
  }
  if (detail.includes("not assigned") || detail.includes("does not provide")) {
    return "Ø§Ù„Ø¯ÙƒØªÙˆØ± Ø§Ù„Ù…Ø®ØªØ§Ø± ØºÙŠØ± Ù…ØªØ§Ø­ Ù„ØªÙ†ÙÙŠØ° Ø§Ù„Ø®Ø¯Ù…Ø© Ø¯ÙŠ. Ø§Ø®ØªØ§Ø± Ø¯ÙƒØªÙˆØ± ØªØ§Ù†ÙŠ Ù„Ù„Ø®Ø¯Ù…Ø©.";
  }
  if (detail.includes("device") && detail.includes("price")) {
    return "Ø³Ø¹Ø± Ø¬Ù‡Ø§Ø² Ø§Ù„Ù„ÙŠØ²Ø± Ø§Ù„Ù…Ø®ØªØ§Ø± ØºÙŠØ± Ù…ÙØ¹Ù‘Ù„ Ù„Ù„Ø®Ø¯Ù…Ø© Ø¯ÙŠ.";
  }
  return error.message;
}

export async function changeAppointmentService(
  _previous: AppointmentServiceChangeState,
  formData: FormData,
): Promise<AppointmentServiceChangeState> {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const serviceId = String(formData.get("service_id") || "");
  const doctorId = String(formData.get("doctor_id") || "").trim();
  const laserDeviceKey = String(formData.get("laser_device_key") || "").trim();
  const startAt = String(formData.get("start_at") || "").trim();
  if (!appointmentId || !serviceId || !doctorId) {
    return { ok: false, error: "Ø§Ø®ØªØ§Ø± Ø§Ù„Ø®Ø¯Ù…Ø© ÙˆØ§Ù„Ø¯ÙƒØªÙˆØ± Ù‚Ø¨Ù„ Ø§Ù„Ø­ÙØ¸." };
  }
  try {
    await tiaRequest(`/booking/appointments/${appointmentId}/service`, {
      method: "POST",
      body: JSON.stringify({
        service_id: serviceId,
        doctor_id: doctorId,
        laser_device_key: laserDeviceKey || null,
        start_at: startAt || null,
      }),
    });
    refreshAppointmentViews(appointmentId, patientId || undefined);
    return { ok: true, error: null };
  } catch (error) {
    return { ok: false, error: serviceChangeError(error) };
  }
}

export async function cancelAppointment(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const reason = String(formData.get("reason") || "").trim();
  const overridePolicy = formData.get("override_policy") === "1" || formData.get("override_policy") === "on";
  if (!appointmentId || !reason) return;
  await tiaRequest(`/booking/appointments/${appointmentId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason, override_policy: overridePolicy }),
  });
  refreshAppointmentViews(appointmentId, patientId || undefined);
}

function moneyToMinor(raw: string) {
  const normalized = raw.trim().replace(",", ".");
  if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) throw new Error("Ø§ÙƒØªØ¨ Ù…Ø¨Ù„Øº ØµØ­ÙŠØ­ Ø¨Ø­Ø¯ Ø£Ù‚ØµÙ‰ Ø±Ù‚Ù…ÙŠÙ† Ø¹Ø´Ø±ÙŠÙŠÙ†.");
  const [whole, fraction = ""] = normalized.split(".");
  return Number(whole) * 100 + Number((fraction + "00").slice(0, 2));
}

export async function recordAppointmentPayment(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const amount = String(formData.get("amount") || "0");
  const discount = String(formData.get("discount") || "0");
  const paymentMethod = String(formData.get("payment_method") || "");
  const externalReference = String(formData.get("external_reference") || "").trim();
  const pulseMode = String(formData.get("pulse_mode") || "none");
  const pulsePackOfferId = String(formData.get("pulse_pack_offer_id") || "").trim();
  if (!appointmentId || !paymentMethod) return;

  try {
    await tiaRequest(`/payments/appointments/${appointmentId}/checkout`, {
      method: "POST",
      headers: { "Idempotency-Key": `dashboard-checkout:${randomUUID()}` },
      body: JSON.stringify({
        amount_minor: moneyToMinor(amount),
        discount_minor: moneyToMinor(discount),
        payment_method: paymentMethod,
        external_reference: externalReference || null,
        pulse_mode: pulseMode,
        pulse_pack_offer_id: pulsePackOfferId || null,
      }),
    });
  } catch (error) {
    const message =
      error instanceof TiaApiError
        ? error.message
        : "ØªØ¹Ø°Ø± ØªØ³Ø¬ÙŠÙ„ Ø§Ù„Ø­Ø³Ø§Ø¨. Ø±Ø§Ø¬Ø¹ Ø·Ø±ÙŠÙ‚Ø© Ø§Ù„Ø­Ø³Ø§Ø¨ ÙˆØ§Ù„Ù…Ø¨Ù„Øº ÙˆØ­Ø§ÙˆÙ„ Ù…Ø±Ø© Ø£Ø®Ø±Ù‰.";
    redirect(`/appointments/${appointmentId}?visit_error=${encodeURIComponent(message)}`);
  }
  refreshAppointmentViews(appointmentId, patientId || undefined);
  redirect(`/appointments/${appointmentId}?visit_saved=payment`);
}

export async function addAppointmentProduct(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const productId = String(formData.get("product_id") || "");
  const unitPrice = String(formData.get("unit_price") || "");
  const quantity = Math.max(1, Number(String(formData.get("quantity") || "1")) || 1);
  if (!appointmentId || !productId || !unitPrice) return;
  await tiaRequest(`/inventory/appointments/${appointmentId}/products`, {
    method: "POST",
    body: JSON.stringify({
      product_id: productId,
      quantity,
      unit_price_minor: moneyToMinor(unitPrice),
    }),
  });
  refreshAppointmentViews(appointmentId, patientId || undefined);
}

export async function removeAppointmentProduct(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const lineId = String(formData.get("line_id") || "");
  if (!appointmentId || !lineId) return;
  await tiaRequest(`/inventory/appointments/${appointmentId}/products/${lineId}`, { method: "DELETE" });
  refreshAppointmentViews(appointmentId, patientId || undefined);
}

function appointmentCommerceError(error: unknown) {
  if (!(error instanceof TiaApiError)) return "ØªØ¹Ø°Ø± Ø­ÙØ¸ ØªØ¹Ø¯ÙŠÙ„ Ø§Ù„Ø²ÙŠØ§Ø±Ø©. Ø­Ø§ÙˆÙ„ Ù…Ø±Ø© Ø£Ø®Ø±Ù‰.";
  const detail = error.technicalMessage || "";
  if (detail.includes("primary appointment service")) return "Ø§Ù„Ø®Ø¯Ù…Ø© Ø§Ù„Ø£Ø³Ø§Ø³ÙŠØ© Ù…ÙˆØ¬ÙˆØ¯Ø© Ø¨Ø§Ù„ÙØ¹Ù„ ÙÙŠ Ø§Ù„Ù…ÙˆØ¹Ø¯.";
  if (detail.includes("already attached")) return "Ø§Ù„Ø®Ø¯Ù…Ø© Ø§Ù„Ø¥Ø¶Ø§ÙÙŠØ© Ù…ÙˆØ¬ÙˆØ¯Ø© Ø¨Ø§Ù„ÙØ¹Ù„ ÙÙŠ Ø§Ù„Ø²ÙŠØ§Ø±Ø©.";
  if (detail.includes("laser device")) return "Ø§Ø®ØªØ§Ø± Ø¬Ù‡Ø§Ø² Ø§Ù„Ù„ÙŠØ²Ø± Ø§Ù„ØµØ­ÙŠØ­ Ù„Ù„Ø®Ø¯Ù…Ø©.";
  if (detail.includes("existing appointment payment")) return "ÙÙŠÙ‡ Ø¯ÙØ¹Ø© Ù…Ø³Ø¬Ù„Ø© Ø¹Ù„Ù‰ Ø§Ù„Ù…ÙˆØ¹Ø¯. Ø±Ø§Ø¬Ø¹Ù‡Ø§ Ø£Ùˆ Ø§Ø³ØªØ±Ø¯Ù‡Ø§ Ø£ÙˆÙ„Ù‹Ø§ Ù‚Ø¨Ù„ ØªØ­ÙˆÙŠÙ„ Ø§Ù„Ø¬Ù„Ø³Ø© Ù„Ø¨Ø§ÙƒÙŠØ¯Ø¬.";
  if (detail.includes("different service") || detail.includes("different laser device")) return "Ø§Ù„Ø¨Ø§ÙƒÙŠØ¯Ø¬ Ø§Ù„Ù…Ø®ØªØ§Ø±Ø© Ù„Ø§ ØªØ·Ø§Ø¨Ù‚ Ø§Ù„Ø®Ø¯Ù…Ø© Ø£Ùˆ Ø¬Ù‡Ø§Ø² Ø§Ù„Ù„ÙŠØ²Ø± ÙÙŠ Ø§Ù„Ù…ÙˆØ¹Ø¯.";
  if (detail.includes("already linked to a package")) return "Ø§Ù„Ø®Ø¯Ù…Ø© Ù…Ø±ØªØ¨Ø·Ø© Ø¨Ø¨Ø§ÙƒÙŠØ¯Ø¬ Ø¨Ø§Ù„ÙØ¹Ù„.";
  if (detail.includes("package-backed additional service")) return "Ø§Ù„Ø®Ø¯Ù…Ø© Ø§Ù„Ø¥Ø¶Ø§ÙÙŠØ© Ù…Ø±ØªØ¨Ø·Ø© Ø¨Ø¨Ø§ÙƒÙŠØ¯Ø¬ØŒ Ù„Ø°Ù„Ùƒ Ù„Ø§ ÙŠÙ…ÙƒÙ† Ø­Ø°ÙÙ‡Ø§ ÙƒØ®Ø¯Ù…Ø© Ø¹Ø§Ø¯ÙŠØ©.";
  return error.message;
}

export async function addAppointmentAdditionalService(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const serviceId = String(formData.get("service_id") || "");
  const laserDeviceKey = String(formData.get("laser_device_key") || "").trim();
  const pulseMode = String(formData.get("pulse_mode") || "none");
  const pulsesUsedRaw = String(formData.get("pulses_used") || "").trim();
  const pulsePackOfferId = String(formData.get("pulse_pack_offer_id") || "").trim();
  if (!appointmentId || !serviceId) return;
  const pulsesUsed = pulsesUsedRaw ? Number(pulsesUsedRaw) : null;
  if (pulsesUsed !== null && (!Number.isInteger(pulsesUsed) || pulsesUsed <= 0)) {
    redirect(`/appointments/${appointmentId}?visit_error=${encodeURIComponent("عدد الـPulses لازم يكون رقم صحيح أكبر من صفر.")}`);
  }
  let errorMessage: string | null = null;
  try {
    await tiaRequest(`/booking/appointments/${appointmentId}/additional-services`, {
      method: "POST",
      headers: { "Idempotency-Key": `appointment-extra-service:${randomUUID()}` },
      body: JSON.stringify({
        service_id: serviceId,
        laser_device_key: laserDeviceKey || null,
        pulse_mode: pulseMode,
        pulses_used: pulsesUsed,
        pulse_pack_offer_id: pulsePackOfferId || null,
      }),
    });
  } catch (error) {
    errorMessage = appointmentCommerceError(error);
  }
  if (errorMessage) redirect(`/appointments/${appointmentId}?visit_error=${encodeURIComponent(errorMessage)}`);
  refreshAppointmentViews(appointmentId, patientId || undefined);
  redirect(`/appointments/${appointmentId}?visit_saved=service`);
}

export async function removeAppointmentAdditionalService(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const lineId = String(formData.get("line_id") || "");
  if (!appointmentId || !lineId) return;
  let errorMessage: string | null = null;
  try {
    await tiaRequest(`/booking/appointments/${appointmentId}/additional-services/${lineId}`, { method: "DELETE" });
  } catch (error) {
    errorMessage = appointmentCommerceError(error);
  }
  if (errorMessage) redirect(`/appointments/${appointmentId}?visit_error=${encodeURIComponent(errorMessage)}`);
  refreshAppointmentViews(appointmentId, patientId || undefined);
  redirect(`/appointments/${appointmentId}?visit_saved=service_removed`);
}

export async function purchasePackageFromAppointment(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const offerId = String(formData.get("offer_id") || "");
  if (!appointmentId || !offerId) return;
  let errorMessage: string | null = null;
  try {
    await tiaRequest(`/booking/appointments/${appointmentId}/package-offer`, {
      method: "POST",
      headers: { "Idempotency-Key": `appointment-package:${randomUUID()}` },
      body: JSON.stringify({ offer_id: offerId }),
    });
  } catch (error) {
    errorMessage = appointmentCommerceError(error);
  }
  if (errorMessage) redirect(`/appointments/${appointmentId}?visit_error=${encodeURIComponent(errorMessage)}`);
  refreshAppointmentViews(appointmentId, patientId || undefined);
  redirect(`/appointments/${appointmentId}?visit_saved=package`);
}

export async function purchasePackageForAdditionalService(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const lineId = String(formData.get("line_id") || "");
  const offerId = String(formData.get("offer_id") || "");
  if (!appointmentId || !lineId || !offerId) return;
  let errorMessage: string | null = null;
  try {
    await tiaRequest(
      `/booking/appointments/${appointmentId}/additional-services/${lineId}/package-offer`,
      {
        method: "POST",
        headers: { "Idempotency-Key": `appointment-extra-package:${lineId}:${randomUUID()}` },
        body: JSON.stringify({ offer_id: offerId }),
      },
    );
  } catch (error) {
    errorMessage = appointmentCommerceError(error);
  }
  if (errorMessage) redirect(`/appointments/${appointmentId}?visit_error=${encodeURIComponent(errorMessage)}`);
  refreshAppointmentViews(appointmentId, patientId || undefined);
  redirect(`/appointments/${appointmentId}?visit_saved=extra_package`);
}

export async function refundAppointmentPayment(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const paymentTransactionId = String(formData.get("payment_transaction_id") || "");
  const amount = String(formData.get("amount") || "");
  const reason = String(formData.get("reason") || "").trim();
  if (!appointmentId || !paymentTransactionId || !amount || !reason) return;
  await tiaRequest(`/payments/appointments/${appointmentId}/refunds`, {
    method: "POST",
    headers: { "Idempotency-Key": `dashboard-refund:${randomUUID()}` },
    body: JSON.stringify({
      payment_transaction_id: paymentTransactionId,
      amount_minor: moneyToMinor(amount),
      reason,
    }),
  });
  refreshAppointmentViews(appointmentId, patientId || undefined);
}

export async function rescheduleAppointment(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const startAt = String(formData.get("start_at") || "");
  if (!appointmentId || !startAt) return;
  const replacement = await tiaRequest<Appointment>(`/booking/appointments/${appointmentId}/reschedule`, {
    method: "POST",
    headers: { "Idempotency-Key": `dashboard:${randomUUID()}` },
    body: JSON.stringify({
      start_at: startAt,
      reason: "appointment_rescheduled_from_operations",
    }),
  });
  revalidatePath("/appointments");
  revalidatePath(`/appointments/${appointmentId}`);
  revalidatePath(`/patients/${replacement.patient_id}`);
  revalidatePath("/analytics");
  redirect(`/appointments/${replacement.id}`);
}
