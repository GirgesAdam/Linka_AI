import { expect, test } from "@playwright/test";

import {
  beginSignup,
  resendSignupConfirmation,
  restartSignup,
  type PendingSignupStore,
  type SignupAuthClient,
} from "../src/app/signup/signup-flow";

class MemoryStore implements PendingSignupStore {
  value = "";
  setCalls: string[] = [];
  clearCalls = 0;

  constructor(value = "") {
    this.value = value;
  }

  async get() { return this.value; }
  async set(email: string) { this.value = email; this.setCalls.push(email); }
  async clear() { this.value = ""; this.clearCalls += 1; }
}

function mockAuth(overrides: Partial<SignupAuthClient> = {}) {
  type SignUpInput = Parameters<SignupAuthClient["signUp"]>[0];
  type ResendInput = Parameters<SignupAuthClient["resend"]>[0];
  const calls = { signUp: [] as SignUpInput[], resend: [] as ResendInput[] };
  const auth: SignupAuthClient = {
    async signUp(input) {
      calls.signUp.push(input);
      return { data: { session: null, user: { identities: [{}] } }, error: null };
    },
    async resend(input) {
      calls.resend.push(input);
      return { error: null };
    },
    ...overrides,
  };
  return { auth, calls };
}

test("unconfirmed signUp runs once, persists normalized email, and enters check-email state", async () => {
  const store = new MemoryStore();
  const { auth, calls } = mockAuth();
  const result = await beginSignup({
    email: " Clinic@Example.com ",
    password: "password123",
    confirmPassword: "password123",
    origin: "https://app.linkaai.online",
  }, { auth, store });

  expect(result.redirectTo).toBe("/signup?step=check-email");
  expect(store.value).toBe("clinic@example.com");
  expect(calls.signUp).toHaveLength(1);
  expect(calls.signUp[0]).toMatchObject({
    email: "clinic@example.com",
    password: "password123",
    options: { emailRedirectTo: "https://app.linkaai.online/auth/callback?next=/onboarding" },
  });
});

test("immediate session clears pending state and continues to onboarding", async () => {
  const store = new MemoryStore("stale@example.com");
  const { auth } = mockAuth({
    async signUp() { return { data: { session: { access_token: "test" }, user: { identities: [{}] } }, error: null }; },
  });
  const result = await beginSignup({
    email: "clinic@example.com",
    password: "password123",
    confirmPassword: "password123",
    origin: "https://app.linkaai.online",
  }, { auth, store });

  expect(result.redirectTo).toBe("/onboarding");
  expect(store.value).toBe("");
  expect(store.clearCalls).toBe(1);
});

test("resend uses signup resend only and never performs a second signup", async () => {
  const store = new MemoryStore("clinic@example.com");
  const { auth, calls } = mockAuth();
  const result = await resendSignupConfirmation({ origin: "https://app.linkaai.online" }, { auth, store });

  expect(result.redirectTo).toContain("/signup?step=check-email&success=");
  expect(calls.resend).toHaveLength(1);
  expect(calls.resend[0]).toMatchObject({
    type: "signup",
    email: "clinic@example.com",
    options: { emailRedirectTo: "https://app.linkaai.online/auth/callback?next=/onboarding" },
  });
  expect(calls.signUp).toHaveLength(0);
});

test("missing pending email fails resend safely and restart clears stale state", async () => {
  const store = new MemoryStore();
  const { auth, calls } = mockAuth();
  const missing = await resendSignupConfirmation({ origin: "https://app.linkaai.online" }, { auth, store });
  expect(missing.redirectTo).toContain("/signup?error=");
  expect(calls.resend).toHaveLength(0);

  store.value = "stale@example.com";
  const restarted = await restartSignup({ auth, store });
  expect(restarted.redirectTo).toBe("/signup");
  expect(store.value).toBe("");
});
