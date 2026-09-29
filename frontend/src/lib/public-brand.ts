const LEGACY_PRODUCT_NAMES = new Set(["Tia", "Tia AI"]);

export function resolvePublicLegalName(value: string | undefined): string {
  const configured = value?.trim();

  if (!configured || LEGACY_PRODUCT_NAMES.has(configured)) {
    return "Linka";
  }

  return configured;
}
