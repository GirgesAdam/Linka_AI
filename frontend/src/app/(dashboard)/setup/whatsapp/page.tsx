import Link from "next/link";
import { CheckCircle2, ExternalLink, Link2, MessageCircleMore } from "lucide-react";
import { redirect } from "next/navigation";

import { PageHeader } from "@/components/page-header";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import {
  WhatsAppDirectOnboarding,
  type WhatsAppSetupState,
} from "../../automations/whatsapp-direct-onboarding";

const setupSteps = [
  {
    title: "جهّز الرقم في Meta",
    description: "افتح Meta App وجهّز رقم العيادة على WhatsApp Cloud API. كل خانة داخل Linka معها رابط مباشر للمكان المطلوب.",
    icon: ExternalLink,
  },
  {
    title: "انسخ البيانات إلى Linka",
    description: "Linka تتحقق من الحساب والرقم والصلاحيات قبل حفظ أي بيانات سرية.",
    icon: CheckCircle2,
  },
  {
    title: "فعّل استقبال الرسائل",
    description: "انسخ رابط الاستقبال ورمز التحقق إلى Meta مرة واحدة، وبعدها Linka تفحص الجاهزية تلقائيًا.",
    icon: Link2,
  },
];

export default async function WhatsAppSetupPage() {
  const ctx = await getAppContext();
  if (ctx.workspace.role !== "admin") redirect("/setup");

  const state = await tiaRequest<WhatsAppSetupState>("/channels/whatsapp/setup");

  return (
    <>
      <PageHeader
        title="ربط WhatsApp"
        description="ربط مباشر مع Meta Cloud API. الإعداد يتم مرة واحدة، وLinka تتحقق من كل خطوة قبل التشغيل."
      />

      <Card className="mb-6 border-teal-200 bg-teal-50/40">
        <CardContent className="p-5">
          <div className="flex items-start gap-3">
            <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-emerald-600 text-white">
              <MessageCircleMore size={22} />
            </span>
            <div>
              <b className="text-lg text-slate-950">الربط مباشر بين Linka وMeta</b>
              <p className="mt-1 text-sm leading-6 text-[var(--muted)]">
                استخدم حساب Meta والرقم الخاصين بالعيادة. Linka ستوضح أين تجد كل قيمة وتتحقق منها قبل الحفظ، من غير ما تعرض لك تعقيد تقني أكثر من المطلوب.
              </p>
            </div>
          </div>

          <div className="mt-5 grid gap-3 lg:grid-cols-3">
            {setupSteps.map((step, index) => {
              const Icon = step.icon;
              return (
                <div key={step.title} className="rounded-2xl border border-white bg-white p-4 shadow-sm">
                  <div className="flex items-center gap-2 font-black text-slate-950">
                    <span className="grid size-7 place-items-center rounded-lg bg-slate-900 text-xs text-white">{index + 1}</span>
                    <Icon size={17} /> {step.title}
                  </div>
                  <p className="mt-2 text-sm leading-6 text-[var(--muted)]">{step.description}</p>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <WhatsAppDirectOnboarding state={state} />

      <div className="mt-6 flex flex-wrap gap-3">
        <Link href="/setup" className={cn(buttonVariants({ variant: "outline" }))}>الرجوع لإعدادات العيادة</Link>
        {state.ready_for_automations && (
          <Link href="/automations" className={cn(buttonVariants())}>افتح الرسائل التلقائية</Link>
        )}
      </div>
    </>
  );
}
