export const CANONICAL_ROOT_URL = "https://linkaai.online";
export const CANONICAL_APP_URL = "https://app.linkaai.online";
export const CANONICAL_APP_HOST = "app.linkaai.online";
export const LEGACY_APP_HOST = "app.tiaai.online";

export function normalizeOrigin(value: string) {
  return value.trim().replace(/\/+$/, "");
}

export function configuredAppOrigin(requestOrigin?: string) {
  const configured = process.env.NEXT_PUBLIC_APP_URL?.trim();
  if (configured) return normalizeOrigin(configured);
  return requestOrigin ? normalizeOrigin(requestOrigin) : undefined;
}

export function authCallbackUrl(origin: string, next: "/onboarding" | "/reset-password") {
  return `${normalizeOrigin(origin)}/auth/callback?next=${next}`;
}

export function legacyDomainRedirectUrl(
  current: URL,
  enabled: boolean,
): URL | null {
  if (!enabled || current.hostname.toLowerCase() !== LEGACY_APP_HOST) return null;

  // Supabase PKCE/token flows may still return to the legacy callback during
  // the compatibility window. Keep that route functional instead of blindly
  // crossing cookie/session boundaries to the new host.
  if (current.pathname === "/auth/callback") return null;

  const target = new URL(current.toString());
  target.protocol = "https:";
  target.hostname = CANONICAL_APP_HOST;
  target.port = "";
  return target;
}
