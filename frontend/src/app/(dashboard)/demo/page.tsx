import Link from "next/link";
import { notFound } from "next/navigation";
import { BarChart3, BotMessageSquare, CalendarDays, CheckCircle2, ContactRound, Inbox, Sparkles } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { isDemoMode } from "@/lib/demo-mode";

const tour = [
  { href: "/agent-demo", title: "جرّب Linka كأنك عميل", description: "اختبر نفس Agent ونفس booking tools المستخدمة في البرودكشن، لكن على بيانات Demo معزولة.", icon: BotMessageSquare, cta: "ابدأ المحادثة" },
  { href: "/appointments", title: "راجع المواعيد", description: "شوف أثر الحجز أو التعديل أو الإلغاء مباشرة بعد المحادثة.", icon: CalendarDays, cta: "افتح المواعيد" },
  { href: "/patients", title: "استكشف العملاء", description: "راجع ملف العميل وتاريخه بعد تنفيذ المحادثة.", icon: ContactRound, cta: "افتح العملاء" },
  { href: "/analytics", title: "شوف أداء العيادة", description: "راجع التقارير والتحليلات على بيانات الـDemo.", icon: BarChart3, cta: "افتح التقارير" },
  { href: "/inbox", title: "راجع الرسائل", description: "شوف المحادثات والـhandoffs من نفس تجربة التشغيل.", icon: Inbox, cta: "افتح الرسائل" },
] as const;

export default async function DemoHomePage() {
  if (!(await isDemoMode())) notFound();
  return (
    <div className="space-y-7">
      <section className="overflow-hidden rounded-[28px] border border-teal-200 bg-gradient-to-l from-teal-950 via-teal-900 to-slate-950 p-6 text-white shadow-sm md:p-8">
        <div className="max-w-3xl">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/10 px-3 py-1.5 text-xs font-bold text-teal-50"><Sparkles size={14} /> Linka Interactive Demo</div>
          <h1 className="text-3xl font-black tracking-tight md:text-4xl">جرّب نفس Linka المستخدمة في البرودكشن</h1>
          <p className="mt-4 max-w-2xl text-sm leading-7 text-slate-200 md:text-base">الاختلاف الوحيد هو إنك داخل workspace تجريبية معزولة. الـAgent والـbooking logic والـAPI نفسهم، علشان نتيجة الـDemo تمثل سلوك البرودكشن فعلًا.</p>
        </div>
      </section>

      <section className="grid gap-3 md:grid-cols-3">
        {["البيانات هنا صناعية وليست بيانات مرضى حقيقيين.", "الحجز والتعديل والإلغاء يعملون داخل بيئة الـDemo فقط.", "سلوك الـAgent نفسه ليس نسخة منفصلة عن البرودكشن."].map((item) => (
          <div key={item} className="flex gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-sm leading-6 text-slate-700"><CheckCircle2 className="mt-0.5 shrink-0 text-teal-700" size={18} />{item}</div>
        ))}
      </section>

      <section>
        <h2 className="mb-4 text-xl font-black text-slate-950">جولة سريعة</h2>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {tour.map(({ href, title, description, icon: Icon, cta }) => (
            <Card key={href} className="flex min-h-52 flex-col">
              <CardHeader><span className="mb-3 grid size-11 place-items-center rounded-2xl bg-teal-50 text-teal-700"><Icon size={20} /></span><CardTitle>{title}</CardTitle><CardDescription>{description}</CardDescription></CardHeader>
              <CardContent className="mt-auto"><Link href={href} className="text-sm font-black text-teal-700 hover:text-teal-900">{cta} ←</Link></CardContent>
            </Card>
          ))}
        </div>
      </section>
    </div>
  );
}
