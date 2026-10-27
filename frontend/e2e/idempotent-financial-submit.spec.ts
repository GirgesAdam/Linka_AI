import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { operationIdempotencyKey } from "../src/lib/tia/operation-idempotency";

const root = resolve(__dirname, "..");
const mutationForm = readFileSync(resolve(root, "src/components/idempotent-mutation-form.tsx"), "utf8");
const appointmentActions = readFileSync(resolve(root, "src/app/(dashboard)/appointments/[appointmentId]/actions.ts"), "utf8");
const appointmentPayment = readFileSync(resolve(root, "src/app/(dashboard)/appointments/[appointmentId]/appointment-payment-form.tsx"), "utf8");
const appointmentPage = readFileSync(resolve(root, "src/app/(dashboard)/appointments/[appointmentId]/page.tsx"), "utf8");
const patientActions = readFileSync(resolve(root, "src/app/(dashboard)/patients/actions.ts"), "utf8");
const packagePanel = readFileSync(resolve(root, "src/app/(dashboard)/patients/[patientId]/package-panel.tsx"), "utf8");
const pulsePanel = readFileSync(resolve(root, "src/app/(dashboard)/patients/[patientId]/pulse-panel.tsx"), "utf8");

function formWithToken(token: string) {
  const formData = new FormData();
  formData.set("operation_token", token);
  return formData;
}

const tokenA = "11111111-1111-4111-8111-111111111111";
const tokenB = "22222222-2222-4222-8222-222222222222";

test("same logical attempt maps repeated submissions to the same backend idempotency key", () => {
  const prefixes = [
    "dashboard-checkout",
    "dashboard-refund",
    "dashboard-package-purchase",
    "dashboard-package-payment",
    "patient-pulse-pack",
    "patient-pulse-payment",
  ];
  for (const prefix of prefixes) {
    const first = operationIdempotencyKey(formWithToken(tokenA), prefix);
    const duplicate = operationIdempotencyKey(formWithToken(tokenA), prefix);
    expect(duplicate).toBe(first);
    expect(operationIdempotencyKey(formWithToken(tokenB), prefix)).not.toBe(first);
  }
});

test("server actions consume the form operation token instead of generating invocation UUIDs", () => {
  expect(appointmentActions).toContain('operationIdempotencyKey(formData, "dashboard-checkout")');
  expect(appointmentActions).toContain('operationIdempotencyKey(formData, "dashboard-refund")');
  expect(patientActions).toContain('operationIdempotencyKey(formData, "dashboard-package-purchase")');
  expect(patientActions).toContain('operationIdempotencyKey(formData, "dashboard-package-payment")');
  expect(patientActions).toContain('operationIdempotencyKey(formData, "patient-pulse-pack")');
  expect(patientActions).toContain('operationIdempotencyKey(formData, "patient-pulse-payment")');
  expect(appointmentActions).not.toContain('`dashboard-checkout:${randomUUID()}`');
  expect(appointmentActions).not.toContain('`dashboard-refund:${randomUUID()}`');
  expect(patientActions).not.toContain('dashboard-package-purchase:${randomUUID()}');
  expect(patientActions).not.toContain('patient-pulse-pack:${randomUUID()}');
});

test("operation token persists through uncertain result and rotates only after confirmed success", () => {
  expect(mutationForm).toContain("window.sessionStorage.getItem(storageKey)");
  expect(mutationForm).toContain("window.sessionStorage.setItem(storageKey, currentToken)");
  const awaitIndex = mutationForm.indexOf("await action(formData)");
  const rotateIndex = mutationForm.indexOf("const nextToken = createClientRequestId()", awaitIndex);
  expect(awaitIndex).toBeGreaterThan(-1);
  expect(rotateIndex).toBeGreaterThan(awaitIndex);
  expect(mutationForm.slice(0, awaitIndex)).not.toContain("const nextToken = createClientRequestId()");
});

test("all required financial and entitlement forms use persistent operation identity and pending protection", () => {
  expect(appointmentPayment).toContain("linka:operation:appointment-payment:${appointmentId}");
  expect(appointmentPayment).toContain("<PendingSubmitButton");
  expect(appointmentPage).toContain("linka:operation:appointment-refund:${transaction.id}");
  expect(appointmentPage).toContain('PendingSubmitButton variant="danger"');

  expect(packagePanel).toContain("linka:operation:package-payment:${item.id}");
  expect(packagePanel).toContain("linka:operation:package-purchase:${patientId}");
  expect((packagePanel.match(/<PendingSubmitButton/g) || []).length).toBeGreaterThanOrEqual(2);

  expect(pulsePanel).toContain("linka:operation:pulse-payment:${pack.id}");
  expect(pulsePanel).toContain("linka:operation:pulse-purchase:${patientId}");
  expect((pulsePanel.match(/<PendingSubmitButton/g) || []).length).toBeGreaterThanOrEqual(2);
});

test("operation token is identity only and rejects malformed client values before API dispatch", () => {
  expect(() => operationIdempotencyKey(formWithToken("predictable"), "dashboard-checkout")).toThrow("Invalid operation identity");
  expect(operationIdempotencyKey(formWithToken(tokenA), "dashboard-checkout")).toHaveLength("dashboard-checkout:".length + tokenA.length);
});
