import { notFound } from "next/navigation";
import { Bot } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { tiaRequest } from "@/lib/tia/api";
import type { Patient } from "@/lib/types";
import { AgentDemoPlayground } from "./playground";

export default async function AgentDemoPage() {
  if (process.env.TIA_DEMO_ENABLED !== "true") notFound();
  const patients = await tiaRequest<Patient[]>("/crm/patients?status=active&limit=25");

  return (
    <>
      <PageHeader
        title="Test Linka"
        description="جرّب الـcustomer agent من البداية للنهاية وتأكد بنفسك إن الحجز والتعديل والإلغاء بيتنفذوا على بيانات الـDemo الحقيقية."
      />
      {!patients.length ? (
        <div className="rounded-3xl border border-amber-200 bg-amber-50 p-6 text-sm text-amber-950">
          <Bot className="mb-3" /> لا يوجد عملاء Demo نشطون. شغّل seed الخاص ببيئة الـDemo أولًا.
        </div>
      ) : <AgentDemoPlayground patients={patients} />}
    </>
  );
}
