"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

function safeDestination(value: string) {
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/login")) return "/dashboard";
  return value;
}

function loginErrorUrl(message: string, destination?: string) {
  const params = new URLSearchParams({ error: message });
  if (destination && destination !== "/dashboard") params.set("next", destination);
  return `/login?${params.toString()}`;
}

async function signInAndOpenWorkspace(email: string, password: string, destination = "/dashboard") {
  const target = safeDestination(destination);
  const supabase = await createClient();
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error || !data.session?.access_token) {
    redirect(loginErrorUrl("الإيميل أو الباسورد مش صحيح.", target));
  }

  const rawApiUrl = process.env.TIA_API_URL || "http://127.0.0.1:8000";
  const apiUrl = (rawApiUrl.startsWith("//") ? `https:${rawApiUrl}` : rawApiUrl).replace(/\/$/, "");

  let response: Response;
  try {
    response = await fetch(`${apiUrl}/api/v1/auth/me`, {
      headers: { Authorization: `Bearer ${data.session.access_token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(12_000),
    });
  } catch {
    await supabase.auth.signOut();
    redirect(loginErrorUrl("تعذر الاتصال بخدمة Linka الآن. جرّب مرة أخرى بعد قليل.", target));
  }

  if (!response.ok) {
    await supabase.auth.signOut();
    redirect(loginErrorUrl("تعذر فتح حساب العيادة الآن. جرّب مرة أخرى بعد قليل.", target));
  }

  const me = (await response.json()) as { workspaces: Array<{ workspace_id: string }> };
  if (!me.workspaces.length) {
    redirect("/onboarding");
  }

  const cookieStore = await cookies();
  const previousWorkspaceId = cookieStore.get("tia_workspace_id")?.value;
  const selectedWorkspaceId = me.workspaces.some((workspace) => workspace.workspace_id === previousWorkspaceId)
    ? previousWorkspaceId!
    : me.workspaces[0].workspace_id;

  cookieStore.set("tia_workspace_id", selectedWorkspaceId, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
  });
  redirect(target);
}

export async function loginAction(formData: FormData) {
  const email = String(formData.get("email") || "").trim();
  const password = String(formData.get("password") || "");
  const destination = safeDestination(String(formData.get("next") || "/dashboard"));
  await signInAndOpenWorkspace(email, password, destination);
}

export async function demoLoginAction() {
  if (process.env.TIA_DEMO_ENABLED !== "true") {
    redirect(`/login?error=${encodeURIComponent("نسخة الـDemo غير مفعلة على هذه البيئة.")}`);
  }
  const email = process.env.TIA_DEMO_EMAIL?.trim();
  const password = process.env.TIA_DEMO_PASSWORD || "";
  if (!email || !password) {
    redirect(`/login?error=${encodeURIComponent("حساب الـDemo غير مجهز بعد.")}`);
  }
  await signInAndOpenWorkspace(email, password, "/demo");
}
