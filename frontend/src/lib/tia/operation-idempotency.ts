const OPERATION_TOKEN_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function operationIdempotencyKey(formData: FormData, prefix: string): string {
  const token = String(formData.get("operation_token") || "").trim();
  if (!OPERATION_TOKEN_RE.test(token)) {
    throw new Error("Invalid operation identity.");
  }
  const key = `${prefix}:${token}`;
  if (key.length > 128) {
    throw new Error("Operation identity is too long.");
  }
  return key;
}

export function isOperationToken(value: string): boolean {
  return OPERATION_TOKEN_RE.test(value);
}
