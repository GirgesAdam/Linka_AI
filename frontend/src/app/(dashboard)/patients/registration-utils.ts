import type { ServicePackageOffer } from "@/lib/types";

export function getActiveOffersForService<T extends Pick<ServicePackageOffer, "service_id" | "is_active">>(
  offers: T[],
  serviceId: string,
): T[] {
  if (!serviceId) return [];
  return offers.filter((offer) => offer.is_active && offer.service_id === serviceId);
}

export function parseEgpAmountToMinor(value: string): number {
  const normalized = value.trim().replace(",", ".");
  if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) {
    throw new Error("Enter a valid EGP amount with at most two decimal places.");
  }
  const [whole, fraction = ""] = normalized.split(".");
  const amountMinor = Number(whole) * 100 + Number((fraction + "00").slice(0, 2));
  if (!Number.isSafeInteger(amountMinor) || amountMinor < 0) {
    throw new Error("Enter a valid EGP amount.");
  }
  return amountMinor;
}
