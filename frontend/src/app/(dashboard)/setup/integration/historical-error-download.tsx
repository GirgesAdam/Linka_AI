"use client";

import { useEffect, useRef, useState } from "react";
import { Download, LoaderCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { fetchHistoricalImportErrorReport } from "@/lib/historical-import-error-download";

export function HistoricalErrorDownload({ batchId }: { batchId: string }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeRequest = useRef<AbortController | null>(null);

  // A new preview unmounts this component. Never save a result from the old batch.
  useEffect(() => () => { activeRequest.current?.abort(); }, [batchId]);

  async function download() {
    if (activeRequest.current) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    setPending(true);
    setError(null);
    try {
      const file = await fetchHistoricalImportErrorReport(batchId, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(file);
      const link = document.createElement("a");
      try {
        link.href = url;
        link.download = "Linka_Historical_Import_Errors.xlsx";
        document.body.appendChild(link);
        link.click();
      } finally {
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(failure instanceof Error ? failure.message : "تعذر تحميل تقرير الأخطاء.");
      }
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        if (!controller.signal.aborted) setPending(false);
      }
    }
  }

  return (
    <div className="space-y-2">
      <Button type="button" size="sm" variant="outline" onClick={download} disabled={pending}>
        {pending ? <LoaderCircle size={15} className="animate-spin" /> : <Download size={15} />}
        {pending ? "جاري تجهيز التقرير..." : "تحميل تقرير الأخطاء Excel"}
      </Button>
      {error && <p role="alert" className="text-xs font-bold text-red-700">{error}</p>}
    </div>
  );
}
