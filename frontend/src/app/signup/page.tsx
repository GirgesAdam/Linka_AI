import Link from "next/link";
import { cookies } from "next/headers";
import { Bot, Building2, KeyRound, Mail } from "lucide-react";

import { SubmitButton } from "@/components/submit-button";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { resendSignupCodeAction, restartSignupAction, signupAction, verifySignupCodeAction } from "./actions";

const PENDING_EMAIL_COOKIE = "linka_pending_signup_email";

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; success?: string; step?: string }>;
}) {
  const { error, success, step } = await searchParams;
  const cookieStore = await cookies();
  const pendingEmail = cookieStore.get(PENDING_EMAIL_COOKIE)?.value || "";
  const verifyStep = step === "verify" && Boolean(pendingEmail);

  return (
    <main className="grid min-h-screen place-items-center bg-[var(--bg)] p-5 sm:p-8" dir="rtl">
      <div className="w-full max-w-lg rounded-3xl border border-[var(--border)] bg-white p-6 shadow-[0_16px_50px_rgba(15,23,42,.06)] sm:p-8">
        <div className="mb-7 flex items-start gap-3">
          <span className="grid size-12 shrink-0 place-items-center rounded-2xl bg-[var(--accent)] text-white"><Bot /></span>
          <div>
            <h1 className="text-2xl font-black">{verifyStep ? "تأكيد البريد الإلكتروني" : "إنشاء حساب Linka"}</h1>
            <p className="mt-1 text-sm leading-6 text-[var(--muted)]">
              {verifyStep ? <>بعتنالك كود مكوّن من 6 أرقام على <span className="font-black text-slate-800" dir="ltr">{pendingEmail}</span></> : "أنشئ حسابك، وبعد التأكيد هنمشي معاك مباشرة في إعداد العيادة خطوة بخطوة."}
            </p>
          </div>
        </div>

        {error && <div role="alert" className="mb-5 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
        {success && <div role="status" aria-live="polite" className="mb-5 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{success}</div>}

        {verifyStep ? (
          <div className="space-y-5">
            <form action={verifySignupCodeAction} className="space-y-4">
              <label className="block space-y-2">
                <span className="text-sm font-semibold">كود التأكيد</span>
                <Input
                  name="token"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  pattern="[0-9]{6}"
                  minLength={6}
                  maxLength={6}
                  required
                  autoFocus
                  placeholder="123456"
                  dir="ltr"
                  className="h-14 text-center text-xl font-black tracking-[0.45em]"
                  aria-describedby="signup-code-hint"
                />
                <span id="signup-code-hint" className="block text-xs font-normal text-[var(--muted)]">اكتب الـ6 أرقام الموجودة في رسالة Linka.</span>
              </label>
              <SubmitButton className="w-full" size="lg" pendingLabel="جارٍ التأكيد...">
                <KeyRound size={18} /> تأكيد
              </SubmitButton>
            </form>
            <div className="grid gap-2 sm:grid-cols-2">
              <form action={resendSignupCodeAction}>
                <SubmitButton type="submit" variant="outline" className="w-full" pendingLabel="جارٍ الإرسال...">
                  <Mail size={16} /> إعادة إرسال الكود
                </SubmitButton>
              </form>
              <form action={restartSignupAction}>
                <Button type="submit" variant="ghost" className="w-full">تغيير البريد الإلكتروني</Button>
              </form>
            </div>
            <p className="text-center text-xs leading-5 text-[var(--muted)]">لو طلبت كود جديد، استخدم أحدث كود وصلك. Supabase قد تمنع إعادة الإرسال المتكرر لفترة قصيرة.</p>
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
