"use server";

import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";

import { authCallbackUrl, configuredAppOrigin } from "@/lib/production-domain";
import { createClient } from "@/lib/supabase/server";

const PENDING_EMAIL_COOKIE = "linka_pending_signup_email";

async function requestOrigin() {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") || requestHeaders.get("host");
  const proto = requestHeaders.get("x-forwarded-proto") || "https";
  return host ? `${proto}://${host}` : undefined;
}

function signupError(message: string) {
  redirect(`/signup?error=${encodeURIComponent(message)}`);
}

async function setPendingEmail(email: string) {
  const cookieStore = await cookies();
  cookieStore.set(PENDING_EMAIL_COOKIE, email, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/signup",
    maxAge: 60 * 60,
  });
}

async function pendingEmail() {
  const cookieStore = await cookies();
  return cookieStore.get(PENDING_EMAIL_COOKIE)?.value?.trim() || "";
}

async function clearPendingEmail() {
  const cookieStore = await cookies();
  cookieStore.set(PENDING_EMAIL_COOKIE, "", {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/signup",
    maxAge: 0,
  });
}

function isRateLimited(error: { status?: number; code?: string } | null) {
  return error?.status === 429 || error?.code === "over_email_send_rate_limit" || error?.code === "over_request_rate_limit";
}

export async function signupAction(formData: FormData) {
  const email = String(formData.get("email") || "").trim().toLowerCase();
  const password = String(formData.get("password") || "");
  const confirmPassword = String(formData.get("confirm_password") || "");

  if (!email) signupError("اكتب بريد إلكتروني صحيح.");
  if (password.length < 8) signupError("كلمة المرور لازم تكون 8 حروف على الأقل.");
  if (password !== confirmPassword) signupError("كلمتا المرور غير متطابقتين.");

  const supabase = await createClient();
  const origin = configuredAppOrigin(await requestOrigin());
  const { data, error } = await supabase.auth.signUp({
    email,
    password,
    options: origin ? { emailRedirectTo: authCallbackUrl(origin, "/onboarding") } : undefined,
  });

  if (error) {
    signupError(isRateLimited(error) ? "تم إرسال محاولة تأكيد مؤخرًا. استنى شوية وحاول تاني." : "تعذر إنشاء الحساب الآن. جرّب مرة أخرى.");
  }

  if (data.session) {
    await clearPendingEmail();
    redirect("/onboarding");
  }

  if (data.user?.identities && data.user.identities.length === 0) {
    signupError("لو البريد ده مسجل بالفعل، سجل دخولك أو استخدم استعادة كلمة المرور.");
  }

  await setPendingEmail(email);
  redirect("/signup?step=verify");
}

export async function verifySignupCodeAction(formData: FormData) {
  const email = await pendingEmail();
  const token = String(formData.get("token") || "").replace(/\D/g, "");
  if (!email) redirect(`/signup?error=${encodeURIComponent("ابدأ إنشاء الحساب من جديد عشان نعرف البريد المطلوب تأكيده.")}`);
  if (!/^\d{6}$/.test(token)) {
    redirect(`/signup?step=verify&error=${encodeURIComponent("اكتب كود التأكيد المكوّن من 6 أرقام.")}`);
  }

  const supabase = await createClient();
  const { data, error } = await supabase.auth.verifyOtp({ email, token, type: "email" });
  if (error || !data.session) {
    redirect(`/signup?step=verify&error=${encodeURIComponent("الكود غير صحيح أو انتهت صلاحيته. جرّب تاني أو اطلب كود جديد.")}`);
  }

  await clearPendingEmail();
  redirect("/onboarding");
}

export async function resendSignupCodeAction() {
  const email = await pendingEmail();
  if (!email) redirect(`/signup?error=${encodeURIComponent("ابدأ إنشاء الحساب من جديد عشان نقدر نبعت كود تأكيد.")}`);

  const supabase = await createClient();
  const origin = configuredAppOrigin(await requestOrigin());
  const { error } = await supabase.auth.resend({
    type: "signup",
    email,
    options: origin ? { emailRedirectTo: authCallbackUrl(origin, "/onboarding") } : undefined,
  });
  if (error) {
    const message = isRateLimited(error)
      ? "استنى شوية قبل ما تطلب كود جديد."
      : "تعذر إرسال كود جديد الآن. جرّب مرة أخرى.";
    redirect(`/signup?step=verify&error=${encodeURIComponent(message)}`);
  }
  redirect(`/signup?step=verify&success=${encodeURIComponent("بعتنالك كود جديد على نفس البريد.")}`);
}

export async function restartSignupAction() {
  await clearPendingEmail();
  redirect("/signup");
}
