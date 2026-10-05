import { authCallbackUrl } from "@/lib/production-domain";

export type SignupAuthError = { status?: number; code?: string } | null;

type SignupUser = { identities?: unknown[] } | null;
type AuthResult = { data: { session: unknown | null; user?: SignupUser }; error: SignupAuthError };
type VerifyResult = { data: { session: unknown | null }; error: SignupAuthError };
type ResendResult = { error: SignupAuthError };

export type SignupAuthClient = {
  signUp(input: { email: string; password: string; options?: { emailRedirectTo: string } }): Promise<AuthResult>;
  verifyOtp(input: { email: string; token: string; type: "email" }): Promise<VerifyResult>;
  resend(input: { type: "signup"; email: string; options?: { emailRedirectTo: string } }): Promise<ResendResult>;
};

export type PendingSignupStore = {
  get(): Promise<string>;
  set(email: string): Promise<void>;
  clear(): Promise<void>;
};

export type SignupFlowDeps = { auth: SignupAuthClient; store: PendingSignupStore };
export type SignupFlowResult = { redirectTo: string };

function signupUrl(message: string) {
  return `/signup?error=${encodeURIComponent(message)}`;
}

function verifyUrl(message: string, kind: "error" | "success" = "error") {
  return `/signup?step=verify&${kind}=${encodeURIComponent(message)}`;
}

function isRateLimited(error: SignupAuthError) {
  return error?.status === 429 || error?.code === "over_email_send_rate_limit" || error?.code === "over_request_rate_limit";
}

function callbackOptions(origin?: string) {
  return origin ? { emailRedirectTo: authCallbackUrl(origin, "/onboarding") } : undefined;
}

export async function beginSignup(
  input: { email: string; password: string; confirmPassword: string; origin?: string },
  deps: SignupFlowDeps,
): Promise<SignupFlowResult> {
  const email = input.email.trim().toLowerCase();
  if (!email) return { redirectTo: signupUrl("اكتب بريد إلكتروني صحيح.") };
  if (input.password.length < 8) return { redirectTo: signupUrl("كلمة المرور لازم تكون 8 حروف على الأقل.") };
  if (input.password !== input.confirmPassword) return { redirectTo: signupUrl("كلمتا المرور غير متطابقتين.") };

  const { data, error } = await deps.auth.signUp({
    email,
    password: input.password,
    options: callbackOptions(input.origin),
  });
  if (error) {
    return {
      redirectTo: signupUrl(isRateLimited(error) ? "تم إرسال محاولة تأكيد مؤخرًا. استنى شوية وحاول تاني." : "تعذر إنشاء الحساب الآن. جرّب مرة أخرى."),
    };
  }
  if (data.session) {
    await deps.store.clear();
    return { redirectTo: "/onboarding" };
  }
  if (data.user?.identities && data.user.identities.length === 0) {
    return { redirectTo: signupUrl("لو البريد ده مسجل بالفعل، سجل دخولك أو استخدم استعادة كلمة المرور.") };
  }

  await deps.store.set(email);
  return { redirectTo: "/signup?step=verify" };
}

export async function verifySignupCode(
  input: { token: string },
  deps: SignupFlowDeps,
): Promise<SignupFlowResult> {
  const email = (await deps.store.get()).trim();
  const token = input.token.replace(/\D/g, "");
  if (!email) return { redirectTo: signupUrl("ابدأ إنشاء الحساب من جديد عشان نعرف البريد المطلوب تأكيده.") };
  if (!/^\d{6}$/.test(token)) {
    return { redirectTo: verifyUrl("اكتب كود التأكيد المكوّن من 6 أرقام.") };
  }

  const { data, error } = await deps.auth.verifyOtp({ email, token, type: "email" });
  if (error || !data.session) {
    return { redirectTo: verifyUrl("الكود غير صحيح أو انتهت صلاحيته. جرّب تاني أو اطلب كود جديد.") };
  }
  await deps.store.clear();
  return { redirectTo: "/onboarding" };
}

export async function resendSignupCode(
  input: { origin?: string },
  deps: SignupFlowDeps,
): Promise<SignupFlowResult> {
  const email = (await deps.store.get()).trim();
  if (!email) return { redirectTo: signupUrl("ابدأ إنشاء الحساب من جديد عشان نقدر نبعت كود تأكيد.") };

  const { error } = await deps.auth.resend({
    type: "signup",
    email,
    options: callbackOptions(input.origin),
  });
  if (error) {
    return {
      redirectTo: verifyUrl(isRateLimited(error) ? "استنى شوية قبل ما تطلب كود جديد." : "تعذر إرسال كود جديد الآن. جرّب مرة أخرى."),
    };
  }
  return { redirectTo: verifyUrl("بعتنالك كود جديد على نفس البريد.", "success") };
}

export async function restartSignup(deps: SignupFlowDeps): Promise<SignupFlowResult> {
  await deps.store.clear();
  return { redirectTo: "/signup" };
}
