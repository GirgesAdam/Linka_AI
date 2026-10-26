export function patientDisplayName(patient: { first_name: string; last_name?: string | null }): string {
  return [patient.first_name, patient.last_name].filter((part) => part?.trim()).join(" ").trim();
}

export const patientSourceOptions = [
  ["whatsapp", "واتساب"],
  ["instagram", "إنستجرام"],
  ["facebook", "فيسبوك"],
  ["website", "الموقع"],
  ["referral", "إحالة"],
  ["walk_in", "زيارة مباشرة"],
  ["campaign", "حملة"],
  ["phone", "هاتف"],
  ["email", "بريد إلكتروني"],
  ["other", "أخرى"],
] as const;
