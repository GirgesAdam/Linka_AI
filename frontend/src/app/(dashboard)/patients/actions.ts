"use server";

import { randomUUID } from "node:crypto";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { TiaApiError, tiaRequest } from "@/lib/tia/api";
import { parseEgpAmountToMinor } from "./registration-utils";

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

export async function updatePatientCustomerId(formData: FormData) {
  const patientId = String(formData.get("patient_id") || "").trim();
  const customerId = String(formData.get("customer_id") || "");
  if (!patientId) return;

  await tiaRequest(`/crm/patients/${patientId}`, {
    method: "PATCH",
    body: JSON.stringify({ customer_id: customerId.trim() ? customerId : null }),
  });
  revalidatePatient(patientId);
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
    headers: { "Idempotency-Key": `dashboard-package-purchase:${randomUUID()}` },
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
    headers: { "Idempotency-Key": `dashboard-package-payment:${randomUUID()}` },
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
    headers: { "Idempotency-Key": `dashboard-package-cancel:${randomUUID()}` },
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
    headers: { "Idempotency-Key": `patient-pulse-pack:${randomUUID()}` },
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
    headers: { "Idempotency-Key": `patient-pulse-payment:${randomUUID()}` },
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
      package: {
        package_id: string;
        offer_id: string | null;
        service_id: string;
        name: string;
        sessions_total: number;
        sessions_remaining: number;
        current_remaining: number;
        sessions_reserved: number;
        sessions_consumed: number;
        replaceable_identity: boolean;
      } | null;
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
    package: { offer_id: string | null; sessions_remaining: number } | null;
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
  const firstName = String(formData.get("first_name") || "").trim();
  const lastName = String(formData.get("last_name") || "").trim();
  const phone = String(formData.get("phone") || "").trim();
  const duplicateAttempt = String(formData.get("duplicate_attempt") || "");
  const customerId = String(formData.get("customer_id") || "").trim();
  const offerId = String(formData.get("initial_package_offer_id") || "").trim();
  const sessionsRaw = String(formData.get("initial_package_sessions_remaining") || "").trim();
  const amountPaidRaw = String(formData.get("initial_package_amount_paid_egp") || "").trim();
  const paymentMethod = String(formData.get("initial_package_payment_method") || "cash");
  const deviceKeys = formData.getAll("initial_pulse_device_key").map((value) => String(value || "").trim());
  const pulseCounts = formData.getAll("initial_pulses_remaining").map((value) => String(value || "").trim());

  if (!firstName) return { error: "\u0623\u062f\u062e\u0644 \u0627\u0644\u0627\u0633\u0645 \u0627\u0644\u0623\u0648\u0644." };
  if (!/^01[0125][0-9]{8}$/.test(phone)) {
    return { error: "\u0631\u0642\u0645 \u0627\u0644\u0645\u0648\u0628\u0627\u064a\u0644 \u064a\u062c\u0628 \u0623\u0646 \u064a\u0643\u0648\u0646 11 \u0631\u0642\u0645\u064b\u0627 \u0648\u064a\u0628\u062f\u0623 \u0628\u0640 010 \u0623\u0648 011 \u0623\u0648 012 \u0623\u0648 015." };
  }
  if (Boolean(offerId) !== Boolean(sessionsRaw)) {
    return { error: "\u0627\u062e\u062a\u0631 \u0627\u0644\u0628\u0627\u0643\u062f\u062c \u0648\u0623\u062f\u062e\u0644 \u0639\u062f\u062f \u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629 \u0645\u0639\u064b\u0627." };
  }
  if (sessionsRaw && (!/^[0-9]+$/.test(sessionsRaw) || Number(sessionsRaw) <= 0)) {
    return { error: "\u0639\u062f\u062f \u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629 \u064a\u062c\u0628 \u0623\u0646 \u064a\u0643\u0648\u0646 \u0639\u062f\u062f\u064b\u0627 \u0635\u062d\u064a\u062d\u064b\u0627 \u0623\u0643\u0628\u0631 \u0645\u0646 \u0635\u0641\u0631." };
  }
  if (offerId && !amountPaidRaw) {
    return { error: "\u0623\u062f\u062e\u0644 \u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u062f\u0641\u0648\u0639\u060c \u0648\u064a\u0645\u0643\u0646 \u0623\u0646 \u064a\u0643\u0648\u0646 \u0635\u0641\u0631\u064b\u0627." };
  }
  if (!offerId && (sessionsRaw || amountPaidRaw)) {
    return { error: "\u0627\u062e\u062a\u0631 \u0627\u0644\u0628\u0627\u0643\u062f\u062c \u0642\u0628\u0644 \u0625\u062f\u062e\u0627\u0644 \u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0623\u0648 \u0627\u0644\u0645\u062f\u0641\u0648\u0639\u0627\u062a." };
  }

  let amountPaidMinor: number | null = null;
  if (offerId) {
    try {
      amountPaidMinor = parseEgpAmountToMinor(amountPaidRaw);
    } catch {
      return { error: "\u0623\u062f\u062e\u0644 \u0645\u0628\u0644\u063a\u064b\u0627 \u0635\u062d\u064a\u062d\u064b\u0627 \u0628\u0627\u0644\u062c\u0646\u064a\u0647 \u0648\u0628\u062d\u062f \u0623\u0642\u0635\u0649 \u0645\u0646 \u062e\u0627\u0646\u062a\u064a\u0646 \u0639\u0634\u0631\u064a\u062a\u064a\u0646." };
    }
    if (!["cash", "visa", "instapay"].includes(paymentMethod)) {
      return { error: "\u0627\u062e\u062a\u0631 \u0637\u0631\u064a\u0642\u0629 \u062f\u0641\u0639 \u0635\u062d\u064a\u062d\u0629." };
    }
  }

  if (deviceKeys.length !== pulseCounts.length) {
    return { error: "\u0623\u0639\u062f \u0645\u0631\u0627\u062c\u0639\u0629 \u0623\u0631\u0635\u062f\u0629 Pulse." };
  }
  const pulseBalances: Array<{ device_key: string; pulses_remaining: number }> = [];
  const seenDevices = new Set<string>();
  for (let index = 0; index < deviceKeys.length; index += 1) {
    const deviceKey = deviceKeys[index];
    const pulsesRaw = pulseCounts[index];
    if (!deviceKey && !pulsesRaw) continue;
    if (!deviceKey || !pulsesRaw) {
      return { error: "\u0627\u062e\u062a\u0631 \u062c\u0647\u0627\u0632 \u0627\u0644\u0644\u064a\u0632\u0631 \u0648\u0623\u062f\u062e\u0644 \u0631\u0635\u064a\u062f\u0647 \u0645\u0639\u064b\u0627." };
    }
    if (!/^[0-9]+$/.test(pulsesRaw) || Number(pulsesRaw) <= 0 || Number(pulsesRaw) > 1_000_000_000) {
      return { error: "\u0639\u062f\u062f \u0627\u0644\u0646\u0628\u0636\u0627\u062a \u064a\u062c\u0628 \u0623\u0646 \u064a\u0643\u0648\u0646 \u0639\u062f\u062f\u064b\u0627 \u0635\u062d\u064a\u062d\u064b\u0627 \u0623\u0643\u0628\u0631 \u0645\u0646 \u0635\u0641\u0631." };
    }
    const normalizedDeviceKey = deviceKey.toLocaleLowerCase();
    if (seenDevices.has(normalizedDeviceKey)) {
      return { error: "\u0644\u0627 \u062a\u0643\u0631\u0631 \u0646\u0641\u0633 \u0627\u0644\u062c\u0647\u0627\u0632 \u0641\u064a \u0623\u0631\u0635\u062f\u0629 Pulse." };
    }
    seenDevices.add(normalizedDeviceKey);
    pulseBalances.push({ device_key: deviceKey, pulses_remaining: Number(pulsesRaw) });
  }

  let patient: { id: string };
  try {
    patient = await tiaRequest<{ id: string }>("/crm/patients/register", {
      method: "POST",
      body: JSON.stringify({
        first_name: firstName,
        last_name: lastName || null,
        phone,
        customer_id: customerId || null,
        source: "walk_in",
        initial_package_offer_id: offerId || null,
        initial_package_sessions_remaining: sessionsRaw ? Number(sessionsRaw) : null,
        initial_package_amount_paid_minor: amountPaidMinor,
        initial_package_payment_method: paymentMethod,
        initial_pulse_balances: pulseBalances,
      }),
    });
  } catch (error) {
    const existingPatient = existingPatientFromRegistrationError(error);
    if (existingPatient) {
      return {
        error: "\u0631\u0642\u0645 \u0627\u0644\u0645\u0648\u0628\u0627\u064a\u0644 \u062f\u0647 \u0645\u0633\u062c\u0644 \u0628\u0627\u0644\u0641\u0639\u0644 \u0644\u0639\u0645\u064a\u0644 \u0645\u0648\u062c\u0648\u062f.",
        existingPatient,
        duplicatePhone: phone,
        duplicateAttempt,
      };
    }
    return {
      error: error instanceof Error ? error.message : "\u062a\u0639\u0630\u0631 \u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u0639\u0645\u064a\u0644. \u062d\u0627\u0648\u0644 \u0645\u0631\u0629 \u0623\u062e\u0631\u0649.",
      existingPatient: null,
    };
  }

  revalidatePath("/patients");
  redirect("/patients/" + patient.id);
}
