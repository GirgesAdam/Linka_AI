"use server";

import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";

import { configuredAppOrigin } from "@/lib/production-domain";
import { createClient } from "@/lib/supabase/server";
import {
  beginSignup,
  resendSignupCode,
  restartSignup,
  type PendingSignupStore,
  type SignupAuthClient,
  verifySignupCode,
} from "./signup-flow";

const PENDING_EMAIL_COOKIE = "linka_pending_signup_email";

async function requestOrigin() {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") || requestHeaders.get("host");
  const proto = requestHeaders.get("x-forwarded-proto") || "https";
  return host ? `${proto}://${host}` : undefined;
}

async function flowDeps() {
  const supabase = await createClient();
  const cookieStore = await cookies();
  const store: PendingSignupStore = {
    get: async () => cookieStore.get(PENDING_EMAIL_COOKIE)?.value?.trim() || "",
    set: async (email) => {
      cookieStore.set(PENDING_EMAIL_COOKIE, email, {
        httpOnly: true,
        sameSite: "lax",
        secure: process.env.NODE_ENV === "production",
        path: "/signup",
        maxAge: 60 * 60,
      });
    },
    clear: async () => {
      cookieStore.set(PENDING_EMAIL_COOKIE, "", {
        httpOnly: true,
        sameSite: "lax",
        secure: process.env.NODE_ENV === "production",
        path: "/signup",
        maxAge: 0,
      });
    },
  };
  const auth: SignupAuthClient = {
    signUp: (input) => supabase.auth.signUp(input),
    verifyOtp: (input) => supabase.auth.verifyOtp(input),
    resend: (input) => supabase.auth.resend(input),
  };
  return { auth, store };
}

export async function signupAction(formData: FormData) {
  const result = await beginSignup(
    {
      email: String(formData.get("email") || ""),
      password: String(formData.get("password") || ""),
      confirmPassword: String(formData.get("confirm_password") || ""),
      origin: configuredAppOrigin(await requestOrigin()),
    },
    await flowDeps(),
  );
  redirect(result.redirectTo);
}

export async function verifySignupCodeAction(formData: FormData) {
  const result = await verifySignupCode(
    { token: String(formData.get("token") || "") },
    await flowDeps(),
  );
  redirect(result.redirectTo);
}

export async function resendSignupCodeAction() {
  const result = await resendSignupCode({ origin: configuredAppOrigin(await requestOrigin()) }, await flowDeps());
  redirect(result.redirectTo);
}

export async function restartSignupAction() {
  const result = await restartSignup(await flowDeps());
  redirect(result.redirectTo);
}
