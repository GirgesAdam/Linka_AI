"use server";

import { revalidatePath } from "next/cache";
import {
  getImportValidationErrorDetail,
  TiaApiError,
  tiaRequest,
  type ImportValidationErrorDetail,
} from "@/lib/tia/api";
import type { HistoricalBatch, HistoricalPreview } from "@/lib/clinic-setup-v2-types";

export type HistoricalImportActionState = {
  preview: HistoricalPreview | null;
  error: string | null;
};

const HISTORICAL_PREVIEW_TIMEOUT_MS = 270_000;
const MAX_FILE_BYTES = 25 * 1024 * 1024;
const MAX_TOTAL_BYTES = 60 * 1024 * 1024;
const MAX_VISIBLE_VALIDATION_ERRORS = 4;

function extension(name: string): "csv" | "xlsx" | null {
  const value = name.toLowerCase();
  if (value.endsWith(".csv")) return "csv";
  if (value.endsWith(".xlsx")) return "xlsx";
  return null;
}

function formatImportValidationError(detail: ImportValidationErrorDetail) {
  const title = detail.error_count === 1
    ? "تعذر استيراد الملف:"
    : `تعذر استيراد الملف بسبب ${detail.error_count} مشاكل:`;
  const visible = detail.errors.slice(0, MAX_VISIBLE_VALIDATION_ERRORS).map((item) =>
    item.row ? `- الصف ${item.row}: ${item.message}` : `- ${item.message}`,
  );
  const hiddenCount = Math.max(detail.error_count - visible.length, 0);
  if (hiddenCount > 0) visible.push(`- وهناك ${hiddenCount} مشاكل إضافية.`);
  return [title, ...visible].join("\n");
}

export async function previewHistoricalImportAction(
  _previous: HistoricalImportActionState,
  formData: FormData,
): Promise<HistoricalImportActionState> {
  try {
    const files = formData.getAll("files").filter((item): item is File => item instanceof File && item.size > 0);
    if (!files.length) throw new Error("اختار ملف Excel أو CSV واحد على الأقل.");
    if (files.length > 10) throw new Error("الحد الأقصى 10 ملفات في عملية استيراد واحدة.");
    const total = files.reduce((sum, file) => sum + file.size, 0);
    if (total > MAX_TOTAL_BYTES) throw new Error("إجمالي الملفات أكبر من 60MB.");

    const documents = [];
    for (const file of files) {
      const format = extension(file.name);
      if (!format) throw new Error(`الملف ${file.name} غير مدعوم. استخدم .xlsx أو .csv.`);
      if (file.size > MAX_FILE_BYTES) throw new Error(`الملف ${file.name} أكبر من 25MB.`);
      documents.push({
        name: file.name,
        format,
        content_base64: Buffer.from(await file.arrayBuffer()).toString("base64"),
      });
    }

    const mode = String(formData.get("mode") || "append") === "replace_previous_imports"
      ? "replace_previous_imports"
      : "append";
    const preview = await tiaRequest<HistoricalPreview>("/clinic/history/preview", {
      method: "POST",
      body: JSON.stringify({ documents, mode }),
    }, { timeoutMs: HISTORICAL_PREVIEW_TIMEOUT_MS });
    revalidatePath("/setup/integration");
    return { preview, error: null };
  } catch (error) {
    if (error instanceof TiaApiError && error.technicalMessage?.startsWith("Request timed out after ")) {
      return {
        preview: null,
        error: "\u0627\u0646\u062a\u0647\u062a \u0645\u0647\u0644\u0629 \u0627\u0646\u062a\u0638\u0627\u0631 \u0645\u0639\u0627\u064a\u0646\u0629 \u0627\u0644\u0645\u0644\u0641. \u0642\u062f \u064a\u0643\u0648\u0646 \u0627\u0644\u062e\u0627\u062f\u0645 \u0645\u0627 \u0632\u0627\u0644 \u064a\u0639\u0627\u0644\u062c \u0627\u0644\u0645\u0639\u0627\u064a\u0646\u0629\u061b \u062d\u062f\u0651\u062b \u0635\u0641\u062d\u0629 \u0627\u0644\u0627\u0633\u062a\u064a\u0631\u0627\u062f \u0648\u062a\u062d\u0642\u0642 \u0645\u0646 \u062d\u0627\u0644\u0629 \u0622\u062e\u0631 \u0639\u0645\u0644\u064a\u0629 \u0642\u0628\u0644 \u0625\u0639\u0627\u062f\u0629 \u0627\u0644\u0645\u062d\u0627\u0648\u0644\u0629 \u0644\u062a\u062c\u0646\u0628 \u0625\u0646\u0634\u0627\u0621 \u0645\u0639\u0627\u064a\u0646\u0629 \u0645\u0643\u0631\u0631\u0629.",
      };
    }
    const validation = getImportValidationErrorDetail(error);
    if (validation) return { preview: null, error: formatImportValidationError(validation) };
    return {
      preview: null,
      error: error instanceof Error ? error.message : "حدث خطأ أثناء معالجة الملف. حاول مرة أخرى.",
    };
  }
}

export async function startHistoricalImportAction(batchId: string): Promise<HistoricalBatch> {
  const result = await tiaRequest<{ batch: HistoricalBatch; import_started: boolean }>(
    `/clinic/history/batches/${batchId}/apply`,
    { method: "POST", body: "{}" },
  );
  revalidatePath("/setup/integration");
  revalidatePath("/patients");
  revalidatePath("/appointments");
  revalidatePath("/analytics");
  return result.batch;
}

export async function readHistoricalImportBatchAction(batchId: string): Promise<HistoricalBatch> {
  const result = await tiaRequest<{ batch: HistoricalBatch; import_started: boolean }>(
    `/clinic/history/batches/${batchId}`,
  );
  return result.batch;
}
