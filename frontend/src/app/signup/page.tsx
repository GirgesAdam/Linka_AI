import Link from "next/link";
import { cookies } from "next/headers";
import { Bot, Building2, Mail } from "lucide-react";

import { SubmitButton } from "@/components/submit-button";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { resendSignupConfirmationAction, restartSignupAction, signupAction } from "./actions";

const PENDING_EMAIL_COOKIE = "linka_pending_signup_email";

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; success?: string; step?: string }>;
}) {
  const { error, success, step } = await searchParams;
  const cookieStore = await cookies();
  const pendingEmail = cookieStore.get(PENDING_EMAIL_COOKIE)?.value || "";
  const checkEmailStep = step === "check-email" && Boolean(pendingEmail);

  return (
    <main className="grid min-h-screen place-items-center bg-[var(--bg)] p-5 sm:p-8" dir="rtl">
      <div className="w-full max-w-lg rounded-3xl border border-[var(--border)] bg-white p-6 shadow-[0_16px_50px_rgba(15,23,42,.06)] sm:p-8">
        <div className="mb-7 flex items-start gap-3">
          <span className="grid size-12 shrink-0 place-items-center rounded-2xl bg-[var(--accent)] text-white"><Bot /></span>
          <div>
            <h1 className="text-2xl font-black">{checkEmailStep ? "راجع بريدك الإلكتروني" : "إنشاء حساب Linka"}</h1>
            <p className="mt-1 text-sm leading-6 text-[var(--muted)]">
              {checkEmailStep ? <>أرسلنا رسالة تأكيد إلى <span className="font-black text-slate-800" dir="ltr">{pendingEmail}</span>. افتح الرسالة واضغط «تأكيد البريد الإلكتروني» لإكمال إنشاء حسابك.</> : "أنشئ حسابك، وبعد التأكيد هنمشي معاك مباشرة في إعداد العيادة خطوة بخطوة."}
            </p>
          </div>
        </div>

        {error && <div role="alert" className="mb-5 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
        {success && <div role="status" aria-live="polite" className="mb-5 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{success}</div>}

        {checkEmailStep ? (
          <div className="space-y-5">
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 text-sm leading-6 text-slate-700">
              لا تحتاج لنسخ أي كود. اضغط رابط التأكيد داخل الرسالة، وLinka هتكمل تسجيل الدخول تلقائيًا.
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <form action={resendSignupConfirmationAction}>
                <SubmitButton type="submit" variant="outline" className="w-full" pendingLabel="جارٍ الإرسال...">
                  <Mail size={16} /> إعادة إرسال رسالة التأكيد
                </SubmitButton>
              </form>
              <form action={restartSignupAction}>
                <Button type="submit" variant="ghost" className="w-full">تغيير البريد الإلكتروني</Button>
              </form>
            </div>
          </div>
        ) : (
          <form action={signupAction} className="space-y-4">
            <label className="block space-y-2">
              <span className="text-sm font-semibold">البريد الإلكتروني</span>
              <Input name="email" type="email" autoComplete="email" required placeholder="name@clinic.com" dir="ltr" />
            </label>
            <label className="block space-y-2">
              <span className="text-sm font-semibold">كلمة المرور</span>
              <Input name="password" type="password" autoComplete="new-password" required minLength={8} dir="ltr" aria-describedby="signup-password-hint" />
              <span id="signup-password-hint" className="block text-xs font-normal text-[var(--muted)]">8 أحرف على الأقل.</span>
            </label>
            <label className="block space-y-2">
              <span className="text-sm font-semibold">تأكيد كلمة المرور</span>
              <Input name="confirm_password" type="password" autoComplete="new-password" required minLength={8} dir="ltr" />
            </label>
            <SubmitButton className="w-full" size="lg" pendingLabel="جارٍ إنشاء الحساب...">
              <Building2 size={18} /> إنشاء الحساب
            </SubmitButton>
          </form>
        )}

        <p className="mt-6 text-center text-sm text-[var(--muted)]">
          عندك حساب بالفعل؟{" "}
          <Link href="/login" className="font-black text-teal-700 hover:underline">تسجيل الدخول</Link>
        </p>
      </div>
    </main>
  );
}
