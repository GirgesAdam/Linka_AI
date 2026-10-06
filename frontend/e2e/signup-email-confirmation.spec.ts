import { expect, test } from "@playwright/test";

const pendingEmailCookie = {
  name: "linka_pending_signup_email",
  value: "signup-test@example.com",
  domain: "127.0.0.1",
  path: "/signup",
  httpOnly: true,
  sameSite: "Lax" as const,
  secure: false,
};

test("signup keeps email and password as the first step", async ({ page }) => {
  await page.goto("/signup");
  await expect(page.getByRole("heading", { name: "إنشاء حساب Linka" })).toBeVisible();
  await expect(page.getByLabel("البريد الإلكتروني")).toHaveAttribute("type", "email");
  await expect(page.locator('input[name="password"]')).toHaveAttribute("type", "password");
  await expect(page.locator('input[name="confirm_password"]')).toHaveAttribute("type", "password");
  await expect(page.getByRole("button", { name: /إنشاء الحساب/ })).toBeVisible();
});

test("check-email state is one-click only with no OTP input", async ({ context, page }) => {
  await context.addCookies([pendingEmailCookie]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/signup?step=check-email");
  await expect(page.getByRole("heading", { name: "راجع بريدك الإلكتروني" })).toBeVisible();
  await expect(page.getByText("signup-test@example.com")).toBeVisible();
  await expect(page.getByText(/لا تحتاج لنسخ أي كود/)).toBeVisible();
  await expect(page.locator('input[name="token"]')).toHaveCount(0);
  await expect(page.locator('[autocomplete="one-time-code"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: /إعادة إرسال رسالة التأكيد/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "تغيير البريد الإلكتروني" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "راجع بريدك الإلكتروني" })).toBeVisible();
  await expect(page.locator('input[name="token"]')).toHaveCount(0);
});
