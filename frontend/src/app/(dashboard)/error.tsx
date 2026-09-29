"use client";

import { CircleAlert, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";

export default function DashboardError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="grid min-h-[55vh] place-items-center" role="alert">
      <div className="max-w-md text-center">
        <span className="mx-auto grid size-12 place-items-center rounded-2xl bg-amber-50 text-amber-700">
          <CircleAlert size={22} />
        </span>
        <h2 className="mt-4 text-xl font-black text-slate-950">تعذر تحميل الصفحة</h2>
        <p className="mt-2 text-sm leading-6 text-slate-500">
          حصلت مشكلة مؤقتة أثناء تحميل البيانات. جرّب مرة أخرى؛ لو المشكلة في جزء واحد من Linka، باقي بيانات العيادة تفضل آمنة كما هي.
        </p>
        <Button className="mt-5" onClick={reset}>
          <RefreshCw size={16} />
          إعادة المحاولة
        </Button>
      </div>
    </div>
  );
}
