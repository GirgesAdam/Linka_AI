import Link from "next/link";
import { Bot, CalendarCheck2, MessagesSquare, ShieldCheck } from "lucide-react";

import { SubmitButton } from "@/components/submit-button";
import { Input } from "@/components/ui/input";
import { loginAction } from "./actions";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; next?: string }>;
}) {
  const { error, next } = await searchParams;
  const continuing = Boolean(next && next.startsWith("/") && !next.startsWith("//"));

  return (
    <main className="grid min-h-screen lg:grid-cols-[1.05fr_.95fr]">
      <section className="hidden bg-[#102a2a] p-12 text-white lg:flex lg:flex-col lg:justify-between">
        <div className="flex items-center gap-3 text-xl font-bold">
          <span className="grid size-11 place-items-center rounded-2xl bg-white/10"><Bot /></span>
          Linka
        </div>
        <div className="max-w-xl">
          <p className="mb-4 text-sm font-semibold text-teal-200">إدارة العيادة وخدمة العملاء</p>
          <h1 className="text-5xl font-black leading-[1.2]">كل شغل العيادة،<br />في مكان واحد.</h1>
          <p className="mt-6 max-w-lg text-lg leading-8 text-slate-300">
            رسائل العملاء، المواعيد، المتابعات والتقارير — مع Linka تساعد الفريق في الشغل اليومي من غير تعقيد.
          </p>
        </div>
        <div className="grid grid-cols-3 gap-3 text-sm text-slate-300">
          <div className="rounded-2xl bg-white/5 p-4"><MessagesSquare className="mb-3 text-teal-300" />الرسائل</div>
          <div className="rounded-2xl bg-white/5 p-4"><CalendarCheck2 className="mb-3 text-teal-300" />المواعيد</div>
          <div className="rounded-2xl bg-white/5 p-4"><ShieldCheck className="mb-3 text-teal-300" />صلاحيات الفريق</div>
        </div>
      </section>

      <section className="flex items-center justify-center p-6 sm:p-12">
        <div className="w-full max-w-md">
          <div className="mb-9 lg:hidden">
            <div className="flex items-center gap-3 text-xl font-bold">
              <span className="grid size-10 place-items-center rounded-xl bg-[var(--accent)] text-white"><Bot size={20} /></span>
              Linka
            </div>
          </div>
          <h2 className="text-3xl font-black">تسجيل الدخول</h2>
          <p className="mt-2 text-sm text-[var(--muted)]">
            {continuing ? "سجّل الدخول للمتابعة من نفس الصفحة اللي كنت فيها." : "ادخل بحسابك، أو أنشئ حساب جديد لو دي أول مرة تستخدم Linka."}
          </p>
          {error && <div role="alert" className="mt-5 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

          <form action={loginAction} className="mt-7 space-y-4">
            {continuing && <input type="hidden" name="next" value={next} />}
            <label className="block space-y-2">
              <span className="text-sm font-semibold">البريد الإلكتروني</span>
              <Input name="email" type="email" autoComplete="email" required placeholder="name@clinic.com" dir="ltr" />
            </label>
            <label className="block space-y-2">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-semibold">كلمة المرور</span>
                <Link href="/forgot-password" className="text-xs font-bold text-teal-700 hover:underline">
                  نسيت كلمة المرور؟
                </Link>
              </div>
              <Input name="password" type="password" autoComplete="current-password" required dir="ltr" />
            </label>
            <SubmitButton className="mt-2 w-full" size="lg" pendingLabel="جارٍ تسجيل الدخول...">تسجيل الدخول</SubmitButton>
          </form>

          <div className="mt-6 rounded-2xl border border-slate-200 bg-slate-50 p-4 text-center text-sm">
            أول مرة تستخدم Linka؟{" "}
            <Link href="/signup" className="font-black text-teal-700 hover:underline">
              أنشئ حساب وابدأ إعداد عيادتك
            </Link>
          </div>
          <p className="mt-4 text-center text-xs leading-5 text-[var(--muted)]">بيانات العيادة محمية ولا تظهر إلا للأعضاء المصرح لهم.</p>
        </div>
      </section>
    </main>
  );
}
