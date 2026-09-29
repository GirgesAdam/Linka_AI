import Link from "next/link";
import { Bot, Building2 } from "lucide-react";

import { SubmitButton } from "@/components/submit-button";
import { Input } from "@/components/ui/input";
import { signupAction } from "./actions";

export default async function SignupPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; success?: string }>;
}) {
  const { error, success } = await searchParams;
  return (
    <main className="grid min-h-screen place-items-center bg-[var(--bg)] p-5 sm:p-8" dir="rtl">
      <div className="w-full max-w-lg rounded-3xl border border-[var(--border)] bg-white p-6 shadow-[0_16px_50px_rgba(15,23,42,.06)] sm:p-8">
        <div className="mb-7 flex items-start gap-3">
          <span className="grid size-12 shrink-0 place-items-center rounded-2xl bg-[var(--accent)] text-white"><Bot /></span>
          <div>
            <h1 className="text-2xl font-black">إنشاء حساب Linka</h1>
            <p className="mt-1 text-sm leading-6 text-[var(--muted)]">
              أنشئ حسابك، وبعد تسجيل الدخول هنمشي معاك مباشرة في إعداد العيادة خطوة بخطوة.
            </p>
          </div>
        </div>

        {error && <div role="alert" className="mb-5 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
        {success && <div role="status" aria-live="polite" className="mb-5 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{success}</div>}

        {!success && (
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
