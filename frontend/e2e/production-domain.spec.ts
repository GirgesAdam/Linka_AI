import { expect, test } from "@playwright/test";

import {
  CANONICAL_APP_URL,
  CANONICAL_ROOT_URL,
  authCallbackUrl,
  configuredAppOrigin,
  legacyDomainRedirectUrl,
} from "../src/lib/production-domain";

test("canonical Linka production domains are linkaai.online", () => {
  expect(CANONICAL_ROOT_URL).toBe("https://linkaai.online");
  expect(CANONICAL_APP_URL).toBe("https://app.linkaai.online");
});

test("configured production origin drives signup and recovery callbacks", () => {
  const previous = process.env.NEXT_PUBLIC_APP_URL;
  process.env.NEXT_PUBLIC_APP_URL = "https://app.linkaai.online/";
  try {
    const origin = configuredAppOrigin("https://app.tiaai.online");
    expect(origin).toBe(CANONICAL_APP_URL);
    expect(authCallbackUrl(origin!, "/onboarding")).toBe(
      "https://app.linkaai.online/auth/callback?next=/onboarding",
    );
    expect(authCallbackUrl(origin!, "/reset-password")).toBe(
      "https://app.linkaai.online/auth/callback?next=/reset-password",
    );
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_APP_URL;
    else process.env.NEXT_PUBLIC_APP_URL = previous;
  }
});

test("legacy host redirect preserves path and query without looping", () => {
  const source = new URL("https://app.tiaai.online/signup?source=old&next=%2Fdashboard");
  const target = legacyDomainRedirectUrl(source, true);
  expect(target?.toString()).toBe(
    "https://app.linkaai.online/signup?source=old&next=%2Fdashboard",
  );
  expect(legacyDomainRedirectUrl(target!, true)).toBeNull();
});

test("legacy auth callback remains functional during compatibility window", () => {
  const callback = new URL(
    "https://app.tiaai.online/auth/callback?code=opaque&next=/reset-password",
  );
  expect(legacyDomainRedirectUrl(callback, true)).toBeNull();
});

test("legacy redirect is disabled until production rollout explicitly enables it", () => {
  expect(
    legacyDomainRedirectUrl(new URL("https://app.tiaai.online/login"), false),
  ).toBeNull();
});
