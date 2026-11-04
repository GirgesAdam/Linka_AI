import type { HistoricalBatch, HistoricalPreview } from "./clinic-setup-v2-types";

const ENTITY_TYPES = new Set([
  "patient",
  "appointment",
  "payment",
  "payment_allocation",
  "package",
  "holding",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function countMap(value: unknown): Record<string, number> | null {
  if (!isRecord(value)) return null;
  const counts: Record<string, number> = {};
  for (const [key, count] of Object.entries(value)) {
    if (!key || typeof count !== "number" || !Number.isSafeInteger(count) || count < 0) return null;
    counts[key] = count;
  }
  return counts;
}

function issueGroups(value: unknown): HistoricalPreview["issue_groups"] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item)) return [];
    const entityType = item.entity_type;
    if (
      typeof item.code !== "string"
      || typeof item.message !== "string"
      || typeof entityType !== "string"
      || !ENTITY_TYPES.has(entityType)
      || typeof item.occurrence_count !== "number"
      || !Number.isSafeInteger(item.occurrence_count)
      || item.occurrence_count < 0
    ) return [];

    const exampleRows = Array.isArray(item.example_rows)
      ? item.example_rows.filter((row): row is number =>
        typeof row === "number" && Number.isSafeInteger(row) && row > 0,
      )
      : [];

    return [{
      code: item.code,
      message: item.message,
      entity_type: entityType as HistoricalPreview["issue_groups"][number]["entity_type"],
      occurrence_count: item.occurrence_count,
      example_rows: exampleRows,
    }];
  });
}

/**
 * Rebuild the detailed preview from the durable batch summary when the
 * original Server Action state is no longer present (for example after reload).
 */
export function historicalPreviewFromBatch(
  batch: HistoricalBatch | null | undefined,
): HistoricalPreview | null {
  if (!batch) return null;

  const readyCounts = countMap(batch.summary?.ready_counts);
  const rejectedCounts = countMap(batch.summary?.rejected_counts);
  if (!readyCounts || !rejectedCounts) return null;

  const projectedCounts = countMap(batch.summary?.projected_counts) ?? {};
  const totalAmount = batch.summary?.total_amount_imported_minor;

  return {
    batch,
    ready_counts: readyCounts,
    rejected_counts: rejectedCounts,
    issue_groups: issueGroups(batch.summary?.issues),
    can_import: Object.values(readyCounts).some((count) => count > 0),
    projected_counts: projectedCounts,
    total_amount_imported_minor:
      typeof totalAmount === "number" && Number.isSafeInteger(totalAmount) && totalAmount >= 0
        ? totalAmount
        : 0,
  };
}