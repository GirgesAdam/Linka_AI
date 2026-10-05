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

export async function signupAction(formData: FormData) {
  const email = String(formData.get("email") || "").trim();
  const password = String(formData.get("password") || "");
  const confirmPassword = String(formData.get("confirm_password") || "");

  if (password.length < 8) {
    redirect(`/signup?error=${encodeURIComponent("كلمة المرور لازم تكون 8 حروف على الأقل.")}`);
  }
  if (password !== confirmPassword) {
    redirect(`/signup?error=${encodeURIComponent("كلمتا المرور غير متطابقتين.")}`);
  }

  const supabase = await createClient();
  const origin = configuredAppOrigin(await requestOrigin());
  const { data, error } = await supabase.auth.signUp({
    email,
    password,
    options: origin
      ? { emailRedirectTo: authCallbackUrl(origin, "/onboarding") }
      : undefined,
  });

  if (error) {
    redirect(`/signup?error=${encodeURIComponent(error.message || "تعذر إنشاء الحساب.")}`);
  }

  if (data.session) {
    redirect("/onboarding");
  }

  redirect(`/signup?success=${encodeURIComponent("تم إنشاء الحساب. افتح رسالة التأكيد في بريدك الإلكتروني، وبعدها هتدخل مباشرة لإعداد العيادة.")}`);
}
