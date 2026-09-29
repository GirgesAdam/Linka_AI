import Link from "next/link";
import { CheckCircle2, Clock3, MessageCircleMore } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { ClinicKnowledgeText } from "@/lib/clinic-knowledge-base-types";
import type { ClinicSetupV2Snapshot, HistoricalBatch } from "@/lib/clinic-setup-v2-types";
import { cn } from "@/lib/utils";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";

import { ClinicSettingsPanel } from "./clinic-settings-panel";

export default async function SetupPage() {
  const [setup, knowledge, history, ctx] = await Promise.all([
    tiaRequest<ClinicSetupV2Snapshot>("/clinic/setup-v2"),
    tiaRequest<ClinicKnowledgeText>("/clinic/knowledge-text"),
    tiaRequest<{ batches: HistoricalBatch[] }>("/clinic/history/batches"),
    getAppContext(),
  ]);
  const admin = ctx.workspace.role === "admin";
  const activeBatch = history.batches.find((batch) => ["importing", "preview_ready", "failed"].includes(batch.status)) || null;

  return (
    <>
      <PageHeader
        title="إعدادات العيادة"
        description="بيانات العيادة، مواعيد العمل، معلومات Linka، والبيانات القديمة في مكان واحد."
      />

      <Card className="mb-5 border-teal-200 bg-teal-50/50">
        <CardContent className="p-5">
          <div className="flex flex-wrap items-center gap-2">
            {setup.readiness.ready ? <CheckCircle2 size={20} className="text-emerald-700" /> : <Clock3 size={20} className="text-amber-700" />}
            <b className="text-lg">{setup.readiness.ready ? "العيادة جاهزة لاستقبال الحجوزات" : `اكتمال البيانات الأساسية ${setup.readiness.progress_percent}%`}</b>
            <Badge tone={setup.readiness.ready ? "green" : "yellow"}>{setup.readiness.ready ? "جاهزة" : "ناقص بيانات"}</Badge>
          </div>
          {!setup.readiness.ready && <p className="mt-2 text-sm text-[var(--muted)]">{setup.readiness.missing.join(" • ")}</p>}
        </CardContent>
      </Card>

      {admin && (
        <Card className="mb-5 border-sky-200 bg-sky-50/50">
          <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-emerald-600 text-white"><MessageCircleMore size={20} /></span>
              <div>
                <b className="text-slate-950">اربط واتساب بالعيادة</b>
                <p className="mt-1 max-w-2xl text-sm leading-6 text-[var(--muted)]">
                  Linka هتمشي معاك خطوة بخطوة في إعداد Meta، وتتحقق من كل البيانات قبل الحفظ، وبعدها تجهز الـWebhook والقوالب للـAutomation.
                </p>
              </div>
            </div>
            <Link href="/setup/whatsapp" className={cn(buttonVariants(), "shrink-0")}>
              ابدأ ربط واتساب
            </Link>
          </CardContent>
        </Card>
      )}

      {admin ? (
        <ClinicSettingsPanel
          setup={setup}
          knowledgeText={knowledge.content}
          historicalBatches={history.batches}
          activeBatch={activeBatch}
        />
      ) : (
        <Card><CardContent className="p-5 text-sm text-[var(--muted)]">إعدادات العيادة متاحة للأدمن فقط.</CardContent></Card>
      )}
    </>
  );
}
