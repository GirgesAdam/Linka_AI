import Link from "next/link";
import { KeyRound } from "lucide-react";

import { SubmitButton } from "@/components/submit-button";
import { Input } from "@/components/ui/input";
import { forgotPasswordAction } from "./actions";

export default async function ForgotPasswordPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; success?: string }>;
}) {
  const { error, success } = await searchParams;
  return (
    <main className="grid min-h-screen place-items-center bg-[var(--bg)] p-5 sm:p-8" dir="rtl">
      <div className="w-full max-w-md rounded-3xl border border-[var(--border)] bg-white p-6 shadow-[0_16px_50px_rgba(15,23,42,.06)] sm:p-8">
        <div className="mb-7 flex items-start gap-3">
          <span className="grid size-12 shrink-0 place-items-center rounded-2xl bg-[var(--accent)] text-white"><KeyRound /></span>
          <div>
            <h1 className="text-2xl font-black">استعادة كلمة المرور</h1>
            <p className="mt-1 text-sm leading-6 text-[var(--muted)]">اكتب البريد المسجل في Linka وهيوصلك رابط آمن لتعيين كلمة مرور جديدة.</p>
          </div>
        </div>

        {error && <div role="alert" className="mb-5 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
        {success && <div role="status" aria-live="polite" className="mb-5 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{success}</div>}

        {!success && (
          <form action={forgotPasswordAction} className="space-y-4">
            <label className="block space-y-2">
              <span className="text-sm font-semibold">البريد الإلكتروني</span>
              <Input name="email" type="email" autoComplete="email" required placeholder="name@clinic.com" dir="ltr" />
            </label>
            <SubmitButton className="w-full" size="lg" pendingLabel="جارٍ إرسال الرابط...">إرسال رابط الاستعادة</SubmitButton>
          </form>
        )}

        <p className="mt-6 text-center text-sm text-[var(--muted)]">
          <Link href="/login" className="font-black text-teal-700 hover:underline">الرجوع لتسجيل الدخول</Link>
        </p>
      </div>
    </main>
  );
}
