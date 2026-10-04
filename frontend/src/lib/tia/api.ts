import "server-only";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";
import { createClient } from "@/lib/supabase/server";
import type { MeResponse } from "@/lib/types";

const RAW_API_URL = process.env.TIA_API_URL || "http://127.0.0.1:8000";
const API_URL = (RAW_API_URL.startsWith("//") ? `https:${RAW_API_URL}` : RAW_API_URL).replace(/\/$/, "");
const REQUEST_TIMEOUT_MS = 15_000;

export class TiaApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public technicalMessage?: string,
  ) {
    super(message);
  }
}

const accessToken = cache(async function accessToken() {
  const supabase = await createClient();
  const { data: claimsData } = await supabase.auth.getClaims();
  if (!claimsData?.claims?.sub) redirect("/login");
  const { data: sessionData } = await supabase.auth.getSession();
  const token = sessionData.session?.access_token;
  if (!token) redirect("/login");
  return token;
});

async function parseTechnicalError(response: Response) {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    return JSON.stringify(body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function userFacingApiError(status: number) {
  if (status === 400 || status === 422) return "تعذر تنفيذ الطلب بالبيانات الحالية. راجع المدخلات وحاول مرة أخرى.";
  if (status === 401) return "انتهت جلسة تسجيل الدخول. سجّل الدخول مرة أخرى للمتابعة.";
  if (status === 403) return "ليس لديك صلاحية لتنفيذ هذا الإجراء.";
  if (status === 404) return "تعذر العثور على البيانات المطلوبة. قد تكون تغيّرت أو حُذفت.";
  if (status === 409) return "تعذر تنفيذ الإجراء بسبب تعارض في البيانات الحالية. حدّث الصفحة وحاول مرة أخرى.";
  if (status === 429) return "تم إرسال طلبات كثيرة خلال وقت قصير. حاول مرة أخرى بعد قليل.";
  if (status >= 500) return "جزء من خدمة Linka غير متاح مؤقتًا. حاول مرة أخرى بعد قليل.";
  return "تعذر إكمال العملية. حاول مرة أخرى.";
}

export async function tiaRawRequest(path: string, init: RequestInit = {}, options?: { workspace?: boolean }) {
  const token = await accessToken();
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (options?.workspace !== false) {
    const cookieStore = await cookies();
    const workspaceId = cookieStore.get("tia_workspace_id")?.value;
    if (!workspaceId) throw new TiaApiError(409, "اختر العيادة التي تريد العمل عليها أولًا.", "Workspace is not selected.");
    headers.set("X-Workspace-ID", workspaceId);
  }

  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/v1${path}`, {
      ...init,
      headers,
      cache: "no-store",
      signal: init.signal || AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    const technicalMessage = error instanceof Error ? error.message : String(error);
    console.error("[Linka API] network request failed", {
      path,
      method: init.method || "GET",
      detail: technicalMessage,
    });
    throw new TiaApiError(
      503,
      "تعذر الاتصال بخدمة Linka مؤقتًا. حاول مرة أخرى؛ لو كانت العملية طويلة قد تكون اكتملت بالفعل.",
      technicalMessage,
    );
  }
  if (!response.ok) {
    const technicalMessage = await parseTechnicalError(response);
    console.error("[Linka API] request failed", {
      path,
      method: init.method || "GET",
      status: response.status,
      detail: technicalMessage,
    });
    throw new TiaApiError(response.status, userFacingApiError(response.status), technicalMessage);
  }
  return response;
}

export async function tiaRequest<T>(path: string, init: RequestInit = {}, options?: { workspace?: boolean }) {
  const response = await tiaRawRequest(path, init, options);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const getMe = cache(function getMe() {
  return tiaRequest<MeResponse>("/auth/me", {}, { workspace: false });
});
