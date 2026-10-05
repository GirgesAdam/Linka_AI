import { expect, test } from "@playwright/test";

import {
  beginSignup,
  resendSignupCode,
  restartSignup,
  type PendingSignupStore,
  type SignupAuthClient,
  verifySignupCode,
} from "../src/app/signup/signup-flow";

class MemoryStore implements PendingSignupStore {
  value = "";
  setCalls: string[] = [];
  clearCalls = 0;

  constructor(value = "") {
    this.value = value;
  }

  async get() {
    return this.value;
  }

  async set(email: string) {
    this.value = email;
    this.setCalls.push(email);
  }

  async clear() {
    this.value = "";
    this.clearCalls += 1;
  }
}

function mockAuth(overrides: Partial<SignupAuthClient> = {}) {
  type SignUpInput = Parameters<SignupAuthClient["signUp"]>[0];
  type VerifyInput = Parameters<SignupAuthClient["verifyOtp"]>[0];
  type ResendInput = Parameters<SignupAuthClient["resend"]>[0];
  const calls = {
    signUp: [] as SignUpInput[],
    verifyOtp: [] as VerifyInput[],
    resend: [] as ResendInput[],
  };
  const auth: SignupAuthClient = {
    async signUp(input) {
      calls.signUp.push(input);
      return { data: { session: null, user: { identities: [{}] } }, error: null };
    },
    async verifyOtp(input) {
      calls.verifyOtp.push(input);
      return { data: { session: { access_token: "test" } }, error: null };
    },
    async resend(input) {
      calls.resend.push(input);
      return { error: null };
    },
    ...overrides,
  };
  return { auth, calls };
}

test("unconfirmed signUp persists pending email and enters verify step", async () => {
  const store = new MemoryStore();
  const { auth, calls } = mockAuth();
  const result = await beginSignup(
    {
      email: " Clinic@Example.com ",
      password: "password123",
      confirmPassword: "password123",
      origin: "https://app.linkaai.online",
    },
    { auth, store },
  );

  expect(result.redirectTo).toBe("/signup?step=verify");
  expect(store.value).toBe("clinic@example.com");
  expect(store.setCalls).toEqual(["clinic@example.com"]);
  expect(calls.signUp).toHaveLength(1);
  expect(calls.signUp[0]).toMatchObject({
    email: "clinic@example.com",
    password: "password123",
    options: { emailRedirectTo: "https://app.linkaai.online/auth/callback?next=/onboarding" },
  });
});

test("valid verifyOtp clears pending state and continues to onboarding", async () => {
  const store = new MemoryStore("clinic@example.com");
  const { auth, calls } = mockAuth();
  const result = await verifySignupCode({ token: "123456" }, { auth, store });

  expect(result.redirectTo).toBe("/onboarding");
  expect(store.value).toBe("");
  expect(store.clearCalls).toBe(1);
  expect(calls.verifyOtp).toEqual([{ email: "clinic@example.com", token: "123456", type: "email" }]);
});

test("invalid verifyOtp stays on verification with friendly error and preserves pending state", async () => {
  const store = new MemoryStore("clinic@example.com");
  const { auth } = mockAuth({
    async verifyOtp() {
      return { data: { session: null }, error: { code: "otp_expired" } };
    },
  });
  const result = await verifySignupCode({ token: "654321" }, { auth, store });

  expect(result.redirectTo).toContain("/signup?step=verify&error=");
  expect(decodeURIComponent(result.redirectTo)).toContain("الكود غير صحيح أو انتهت صلاحيته");
  expect(store.value).toBe("clinic@example.com");
  expect(store.clearCalls).toBe(0);
});

test("resend uses signup resend only and does not perform a second signup", async () => {
  const store = new MemoryStore("clinic@example.com");
  const { auth, calls } = mockAuth();
  const result = await resendSignupCode({ origin: "https://app.linkaai.online" }, { auth, store });

  expect(result.redirectTo).toContain("/signup?step=verify&success=");
  expect(calls.resend).toHaveLength(1);
  expect(calls.resend[0]).toMatchObject({
    type: "signup",
    email: "clinic@example.com",
    options: { emailRedirectTo: "https://app.linkaai.online/auth/callback?next=/onboarding" },
  });
  expect(calls.signUp).toHaveLength(0);
  expect(calls.verifyOtp).toHaveLength(0);
  expect(store.value).toBe("clinic@example.com");
});

test("missing pending signup state fails safely and restart clears stale state", async () => {
  const store = new MemoryStore();
  const { auth, calls } = mockAuth();
  const missing = await verifySignupCode({ token: "123456" }, { auth, store });

  expect(missing.redirectTo).toContain("/signup?error=");
  expect(calls.verifyOtp).toHaveLength(0);

  store.value = "stale@example.com";
  const restarted = await restartSignup({ auth, store });
  expect(restarted.redirectTo).toBe("/signup");
  expect(store.value).toBe("");
  expect(store.clearCalls).toBe(1);
});
