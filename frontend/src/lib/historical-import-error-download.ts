const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

export async function fetchHistoricalImportErrorReport(
  batchId: string,
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
): Promise<Blob> {
  if (signal.aborted) throw new DOMException("تم إلغاء التحميل.", "AbortError");
  const response = await fetcher(
    "/api/clinic-history-errors/" + encodeURIComponent(batchId),
    { signal, cache: "no-store" },
  );
  if (signal.aborted) throw new DOMException("تم إلغاء التحميل.", "AbortError");
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(
      typeof detail?.error === "string" && detail.error
        ? detail.error
        : "تعذر تحميل تقرير الأخطاء. حاول مرة أخرى.",
    );
  }
  const contentType = response.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  if (contentType !== XLSX_MIME) {
    throw new Error("استجابة تقرير الأخطاء غير صالحة. أعد المحاولة.");
  }
  const file = await response.blob();
  const signature = new Uint8Array(await file.slice(0, 4).arrayBuffer());
  if (
    signature.length !== 4
    || signature[0] !== 0x50
    || signature[1] !== 0x4b
    || signature[2] !== 0x03
    || signature[3] !== 0x04
  ) {
    throw new Error("ملف تقرير الأخطاء غير صالح. أعد المحاولة.");
  }
  if (signal.aborted) throw new DOMException("تم إلغاء التحميل.", "AbortError");
  return file;
}
