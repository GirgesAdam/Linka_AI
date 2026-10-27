"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { TiaApiError, tiaRequest } from "@/lib/tia/api";
import { parseEgpAmountToMinor } from "./registration-utils";
import { operationIdempotencyKey } from "@/lib/tia/operation-idempotency";

function revalidatePatient(patientId: string) {
  revalidatePath(`/patients/${patientId}`);
  revalidatePath("/patients");
}

function revalidatePackageViews(patientId: string) {
  revalidatePatient(patientId);
  revalidatePath("/dashboard");
  revalidatePath("/finance");
  revalidatePath("/analytics");
}

function moneyToMinor(raw: string) {
  const normalized = raw.trim().replace(",", ".");
  if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) {
    throw new Error("اكتب مبلغ صحيح بحد أقصى رقمين عشريين.");
  }
  const [whole, fraction = ""] = normalized.split(".");
  return Number(whole) * 100 + Number((fraction + "00").slice(0, 2));
}

export async function addPatientNote(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "");
  const content = String(formData.get("content") || "").trim();
  if (!patientId || !content) return;

  await tiaRequest(`/crm/patients/${patientId}/notes`, {
    method: "POST",
    body: JSON.stringify({
      content,
      is_pinned: true,
    }),
  });
  revalidatePatient(patientId);
}

export async function updatePatientProfileAction(
  patientId: string,
  changes: {
    name?: string;
    phone?: string | null;
    customer_id?: string | null;
    source?: string;
  },
) {
  if (!patientId) return { ok: false, error: "تعذر تحديث بيانات العميل." };
  const payload: Record<string, string | null> = {};
  if (Object.hasOwn(changes, "name")) {
    const name = String(changes.name || "").trim();
    if (!name) return { ok: false, error: "أدخل اسم العميل." };
    payload.first_name = name;
    payload.last_name = null;
  }
  if (Object.hasOwn(changes, "phone")) payload.phone = changes.phone?.trim() || null;
  if (Object.hasOwn(changes, "customer_id")) payload.customer_id = changes.customer_id?.trim() || null;
  if (Object.hasOwn(changes, "source")) payload.source = changes.source || "other";
  if (Object.keys(payload).length === 0) return { ok: true, error: null };

  try {
    await tiaRequest(`/crm/patients/${patientId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    revalidatePatient(patientId);
    return { ok: true, error: null };
  } catch (error) {
    return { ok: false, error: safeTiaActionError(error, "تعذر تحديث بيانات العميل.") };
  }
}

export async function setPatientWhatsappOptIn(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "").trim();
  const whatsappOptIn = String(formData.get("whatsapp_opt_in") || "false") === "true";
  if (!patientId) return;

  await tiaRequest(`/crm/patients/${patientId}`, {
    method: "PATCH",
    body: JSON.stringify({ whatsapp_opt_in: whatsappOptIn }),
  });
  revalidatePatient(patientId);
}

export async function createPatientTask(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "");
  const title = String(formData.get("title") || "").trim();
  const dueAt = String(formData.get("due_at") || "");
  const conversationId = String(formData.get("conversation_id") || "");
  if (!patientId || !title || !dueAt) return;

  await tiaRequest("/crm/tasks", {
    method: "POST",
    body: JSON.stringify({
      patient_id: patientId,
      conversation_id: conversationId || null,
      assigned_user_id: null,
      task_type: "follow_up",
      execution_mode: "human",
      priority: "normal",
      title,
      due_at: dueAt,
    }),
  });
  revalidatePatient(patientId);
  revalidatePath("/tasks");
}

export async function purchasePatientPackage(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "").trim();
  const offerId = String(formData.get("offer_id") || "").trim();
  const initialPayment = String(formData.get("initial_payment") || "0").trim() || "0";
  const requestedPaymentMethod = String(formData.get("payment_method") || "").trim();
  if (!patientId || !offerId) return;

  const amountPaidMinor = moneyToMinor(initialPayment);
  const paymentMethod = amountPaidMinor > 0 ? requestedPaymentMethod : "unknown";
  if (amountPaidMinor > 0 && !paymentMethod) return;

  await tiaRequest("/booking/package-offers/purchase", {
    method: "POST",
    headers: { "Idempotency-Key": operationIdempotencyKey(formData, "dashboard-package-purchase") },
    body: JSON.stringify({
      patient_id: patientId,
      offer_id: offerId,
      amount_paid_minor: amountPaidMinor,
      payment_method: paymentMethod,
    }),
  });
  revalidatePackageViews(patientId);
}

export async function recordPatientPackagePayment(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "").trim();
  const packageId = String(formData.get("package_id") || "").trim();
  const amount = String(formData.get("amount") || "").trim();
  const paymentMethod = String(formData.get("payment_method") || "").trim();
  if (!patientId || !packageId || !amount || !paymentMethod) return;

  await tiaRequest(`/booking/patient-packages/${packageId}/payments`, {
    method: "POST",
    headers: { "Idempotency-Key": operationIdempotencyKey(formData, "dashboard-package-payment") },
    body: JSON.stringify({
      amount_minor: moneyToMinor(amount),
      payment_method: paymentMethod,
      external_reference: null,
    }),
  });
  revalidatePackageViews(patientId);
}


export async function cancelPatientPackage(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "").trim();
  const packageId = String(formData.get("package_id") || "").trim();
  const settlementTarget = String(formData.get("settlement_target") || "0").trim() || "0";
  const paymentMethod = String(formData.get("payment_method") || "cash").trim() || "cash";
  const reason = "تم إلغاء الباكيدج من ملف العميل";
  if (!patientId || !packageId) return;

  await tiaRequest(`/booking/patient-packages/${packageId}/cancel-refund`, {
    method: "POST",
    headers: { "Idempotency-Key": operationIdempotencyKey(formData, "dashboard-package-cancel") },
    body: JSON.stringify({
      reason,
      settlement_target_minor: moneyToMinor(settlementTarget),
      payment_method: paymentMethod,
    }),
  });
  revalidatePackageViews(patientId);
}

export async function purchasePatientPulsePack(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "").trim();
  const offerId = String(formData.get("offer_id") || "").trim();
  const amountPaid = moneyToMinor(String(formData.get("amount_paid") || "0"));
  const paymentMethod = String(formData.get("payment_method") || "unknown");
  if (!patientId || !offerId) return;

  await tiaRequest("/booking/pulse-pack-offers/purchase", {
    method: "POST",
    headers: { "Idempotency-Key": operationIdempotencyKey(formData, "patient-pulse-pack") },
    body: JSON.stringify({
      patient_id: patientId,
      offer_id: offerId,
      amount_paid_minor: amountPaid,
      payment_method: amountPaid > 0 ? paymentMethod : "unknown",
    }),
  });
  revalidatePackageViews(patientId);
  revalidatePath("/appointments");
}

export async function recordPatientPulsePackPayment(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "").trim();
  const packId = String(formData.get("pack_id") || "").trim();
  const amountMinor = moneyToMinor(String(formData.get("amount") || ""));
  const paymentMethod = String(formData.get("payment_method") || "cash");
  if (!patientId || !packId || amountMinor <= 0) return;

  await tiaRequest(`/booking/patient-pulse-packs/${packId}/payments`, {
    method: "POST",
    headers: { "Idempotency-Key": operationIdempotencyKey(formData, "patient-pulse-payment") },
    body: JSON.stringify({ amount_minor: amountMinor, payment_method: paymentMethod }),
  });
  revalidatePackageViews(patientId);
}


function safeTiaActionError(error: unknown, fallback: string) {
  if (error instanceof TiaApiError && typeof error.detail === "string") return error.detail;
  return error instanceof Error ? error.message : fallback;
}


export async function loadPatientOpeningDataAction(patientId: string) {
  if (!patientId) return { data: null, error: "تعذر تحميل بيانات العميل." };
  try {
    const data = await tiaRequest<{
      patient_id: string;
      customer_id: string | null;
      packages: Array<{
        package_id: string;
        offer_id: string | null;
        service_id: string;
        name: string;
        sessions_total: number;
        opening_sessions_remaining: number;
        current_remaining: number;
        sessions_reserved: number;
        sessions_consumed: number;
        replaceable_identity: boolean;
      }>;
      pulse_balances: Array<{
        opening_pack_id: string;
        device_key: string;
        device_name: string;
        opening_pulses: number;
        current_remaining: number;
        pulses_consumed: number;
        replaceable_identity: boolean;
      }>;
    }>(`/crm/patients/${patientId}/opening-data`);
    return { data, error: null };
  } catch (error) {
    return { data: null, error: safeTiaActionError(error, "تعذر تحميل بيانات العميل.") };
  }
}

export async function savePatientOpeningDataAction(
  patientId: string,
  payload: {
    customer_id: string | null;
    packages: Array<{ package_id: string | null; offer_id: string | null; sessions_remaining: number }>;
    pulse_balances: Array<{ device_key: string; pulses_remaining: number }>;
  },
) {
  if (!patientId) return { data: null, error: "تعذر تحديث بيانات العميل." };
  try {
    const data = await tiaRequest<Awaited<ReturnType<typeof loadPatientOpeningDataAction>>["data"]>(
      `/crm/patients/${patientId}/opening-data`,
      { method: "PUT", body: JSON.stringify(payload) },
    );
    revalidatePatient(patientId);
    return { data, error: null };
  } catch (error) {
    return { data: null, error: safeTiaActionError(error, "تعذر تحديث بيانات العميل.") };
  }
}


type RegistrationExistingPatient = { id: string; name: string };

type RegistrationActionState = {
  error: string | null;
  existingPatient?: RegistrationExistingPatient | null;
  duplicatePhone?: string | null;
  duplicateAttempt?: string | null;
};

function existingPatientFromRegistrationError(error: unknown): RegistrationExistingPatient | null {
  if (!(error instanceof TiaApiError) || error.status !== 409) return null;
  if (!error.detail || typeof error.detail !== "object" || Array.isArray(error.detail)) return null;
  const detail = error.detail as Record<string, unknown>;
  if (detail.code !== "patient_phone_exists") return null;
  if (typeof detail.patient_id !== "string" || typeof detail.patient_name !== "string") return null;
  return { id: detail.patient_id, name: detail.patient_name };
}

export async function registerPatientAction(
  _previousState: RegistrationActionState,
  formData: FormData,
): Promise<RegistrationActionState> {
  const name = String(formData.get("name") || "").trim();
  const phone = String(formData.get("phone") || "").trim();
  const duplicateAttempt = String(formData.get("duplicate_attempt") || "");
  const customerId = String(formData.get("customer_id") || "").trim();
  const packagesJson = String(formData.get("initial_packages_json") || "[]");
  const deviceKeys = formData.getAll("initial_pulse_device_key").map((value) => String(value || "").trim());
  const pulseCounts = formData.getAll("initial_pulses_remaining").map((value) => String(value || "").trim());

  if (!name) return { error: "أدخل الاسم." };
  if (!/^01[0125][0-9]{8}$/.test(phone)) {
    return { error: "رقم الموبايل يجب أن يكون 11 رقمًا ويبدأ بـ 010 أو 011 أو 012 أو 015." };
  }

  let rawPackages: unknown;
  try {
    rawPackages = JSON.parse(packagesJson);
  } catch {
    return { error: "أعد مراجعة بيانات الباكيدجات." };
  }
  if (!Array.isArray(rawPackages) || rawPackages.length > 20) {
    return { error: "أعد مراجعة بيانات الباكيدجات." };
  }

  const initialPackages: Array<{
    offer_id: string;
    sessions_remaining: number;
    amount_paid_minor: number;
    payment_method: string;
  }> = [];
  const seenOffers = new Set<string>();
  for (const raw of rawPackages) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return { error: "أعد مراجعة بيانات الباكيدجات." };
    }
    const row = raw as Record<string, unknown>;
    const offerId = String(row.offer_id || "").trim();
    const sessionsRaw = String(row.sessions_remaining || "").trim();
    const amountPaidRaw = String(row.amount_paid_egp ?? "").trim();
    const paymentMethod = String(row.payment_method || "cash").trim();
    if (!offerId && !sessionsRaw && !amountPaidRaw) continue;
    if (!offerId || !sessionsRaw || !amountPaidRaw) {
      return { error: "اختر الباكيدج وأدخل الجلسات المتبقية والمبلغ المدفوع لكل باكيدج." };
    }
    if (!/^[0-9]+$/.test(sessionsRaw) || Number(sessionsRaw) <= 0 || Number(sessionsRaw) > 10_000) {
      return { error: "عدد الجلسات المتبقية يجب أن يكون عددًا صحيحًا أكبر من صفر." };
    }
    if (seenOffers.has(offerId)) {
      return { error: "لا يمكن إضافة نفس الباكيدج مرتين." };
    }
    seenOffers.add(offerId);
    let amountPaidMinor: number;
    try {
      amountPaidMinor = parseEgpAmountToMinor(amountPaidRaw);
    } catch {
      return { error: "أدخل مبلغًا صحيحًا بالجنيه وبحد أقصى من خانتين عشريتين لكل باكيدج." };
    }
    if (!["cash", "visa", "instapay"].includes(paymentMethod)) {
      return { error: "اختر طريقة دفع صحيحة لكل باكيدج." };
    }
    initialPackages.push({
      offer_id: offerId,
      sessions_remaining: Number(sessionsRaw),
      amount_paid_minor: amountPaidMinor,
      payment_method: paymentMethod,
    });
  }

  if (deviceKeys.length !== pulseCounts.length) {
    return { error: "أعد مراجعة أرصدة Pulse." };
  }
  const pulseBalances: Array<{ device_key: string; pulses_remaining: number }> = [];
  const seenDevices = new Set<string>();
  for (let index = 0; index < deviceKeys.length; index += 1) {
    const deviceKey = deviceKeys[index];
    const pulsesRaw = pulseCounts[index];
    if (!deviceKey && !pulsesRaw) continue;
    if (!deviceKey || !pulsesRaw) {
      return { error: "اختر جهاز الليزر وأدخل رصيده معًا." };
    }
    if (!/^[0-9]+$/.test(pulsesRaw) || Number(pulsesRaw) <= 0 || Number(pulsesRaw) > 1_000_000_000) {
      return { error: "عدد النبضات يجب أن يكون عددًا صحيحًا أكبر من صفر." };
    }
    const normalizedDeviceKey = deviceKey.toLocaleLowerCase();
    if (seenDevices.has(normalizedDeviceKey)) {
      return { error: "لا تكرر نفس الجهاز في أرصدة Pulse." };
    }
    seenDevices.add(normalizedDeviceKey);
    pulseBalances.push({ device_key: deviceKey, pulses_remaining: Number(pulsesRaw) });
  }

  let patient: { id: string };
  try {
    patient = await tiaRequest<{ id: string }>("/crm/patients/register", {
      method: "POST",
      body: JSON.stringify({
        first_name: name,
        last_name: null,
        phone,
        customer_id: customerId || null,
        source: "walk_in",
        initial_packages: initialPackages,
        initial_pulse_balances: pulseBalances,
      }),
    });
  } catch (error) {
    const existingPatient = existingPatientFromRegistrationError(error);
    if (existingPatient) {
      return {
        error: "رقم الموبايل ده مسجل بالفعل لعميل موجود.",
        existingPatient,
        duplicatePhone: phone,
        duplicateAttempt,
      };
    }
    return {
      error: error instanceof Error ? error.message : "تعذر تسجيل العميل. حاول مرة أخرى.",
      existingPatient: null,
    };
  }

  revalidatePath("/patients");
  redirect("/patients/" + patient.id);
}
