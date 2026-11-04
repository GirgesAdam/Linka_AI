export function createRequestSignal(
  callerSignal: AbortSignal | null | undefined,
  timeoutMs: number,
): AbortSignal {
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  return callerSignal ? AbortSignal.any([callerSignal, timeoutSignal]) : timeoutSignal;
}
