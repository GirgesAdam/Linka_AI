import { TiaApiError, tiaRawRequest } from "@/lib/tia/api";

export const maxDuration = 120;

const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ batchId: string }> },
) {
  const { batchId } = await params;
  if (!UUID_PATTERN.test(batchId)) {
    return Response.json({ error: "معرّف عملية الفحص غير صالح." }, { status: 400 });
  }

  try {
    // Authentication and the current workspace stay on the server.
    const upstream = await tiaRawRequest(
      "/clinic/history/batches/" + batchId + "/rejected-report",
      {},
      { timeoutMs: 90_000 },
    );
    return new Response(upstream.body, {
      status: 200,
      headers: {
        "Content-Type": XLSX_MIME,
        "Content-Disposition": 'attachment; filename="Linka_Historical_Import_Errors.xlsx"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch (error) {
    const status = error instanceof TiaApiError && [401, 403, 404].includes(error.status)
      ? error.status
      : 502;
    return Response.json({
      error: error instanceof TiaApiError
        ? error.message
        : "تعذر تحميل تقرير الأخطاء. حاول مرة أخرى.",
    }, {
      status,
      headers: { "Cache-Control": "no-store" },
    });
  }
}
