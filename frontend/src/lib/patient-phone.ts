export const PATIENT_PHONE_ERROR = "رقم التليفون مش صحيح. لازم يكون 11 رقم.";

export function isValidPatientPhone(value: string) {
  return /^\d{11}$/.test(value.trim());
}
