"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { authCallbackUrl, configuredAppOrigin } from "@/lib/production-domain";
import { createClient } from "@/lib/supabase/server";

async function requestOrigin() {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") || requestHeaders.get("host");
  const proto = requestHeaders.get("x-forwarded-proto") || "https";
  return host ? `${proto}://${host}` : undefined;
}

export async function forgotPasswordAction(formData: FormData) {
  const email = String(formData.get("email") || "").trim();
  if (!email) {
    redirect(`/forgot-password?error=${encodeURIComponent("اكتب البريد الإلكتروني المسجل.")}`);
  }

  const origin = configuredAppOrigin(await requestOrigin());
  if (!origin) {
    redirect(`/forgot-password?error=${encodeURIComponent("تعذر تجهيز رابط الاستعادة. حاول مرة أخرى.")}`);
  }

  const supabase = await createClient();
  const { error } = await supabase.auth.resetPasswordForEmail(email, {
    redirectTo: authCallbackUrl(origin, "/reset-password"),
  });

  if (error) {
    redirect(`/forgot-password?error=${encodeURIComponent("تعذر إرسال رابط الاستعادة الآن. حاول مرة أخرى.")}`);
  }

  redirect(`/forgot-password?success=${encodeURIComponent("لو البريد مسجل عندنا، هيوصلك رابط لتعيين كلمة مرور جديدة.")}`);
}
