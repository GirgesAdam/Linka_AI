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

test("verification step is refresh-safe and mobile usable", async ({ context, page }) => {
  await context.addCookies([pendingEmailCookie]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/signup?step=verify");
  await expect(page.getByRole("heading", { name: "تأكيد البريد الإلكتروني" })).toBeVisible();
  await expect(page.getByText("signup-test@example.com")).toBeVisible();
  const code = page.getByLabel("كود التأكيد");
  await expect(code).toHaveAttribute("inputmode", "numeric");
  await expect(code).toHaveAttribute("autocomplete", "one-time-code");
  await expect(code).toHaveAttribute("maxlength", "6");
  await expect(page.getByRole("button", { name: /^تأكيد$/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /إعادة إرسال الكود/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "تغيير البريد الإلكتروني" })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: "تأكيد البريد الإلكتروني" })).toBeVisible();
  await expect(code).toBeVisible();
});
