import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createServer } from "node:http";
import { createRequestSignal } from "../src/lib/tia/request-signal.ts";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const api = read("../src/lib/tia/api.ts");
const actions = read("../src/app/(dashboard)/setup/integration/actions.ts");
const setupPage = read("../src/app/(dashboard)/setup/page.tsx");

function waitForAbort(signal) {
  if (signal.aborted) return Promise.resolve(signal.reason);
  return new Promise((resolve) => {
    // AbortSignal.timeout uses an unref'ed timer in Node 22. Keep the test
    // event loop alive until the abort event is observed.
    const keepAlive = setTimeout(() => {}, 1_000);
    const onAbort = () => {
      clearTimeout(keepAlive);
      resolve(signal.reason);
    };
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) onAbort();
  });
}

test("global API timeout remains 15 seconds and both request APIs accept timeoutMs", () => {
  assert.match(api, /const REQUEST_TIMEOUT_MS = 15_000;/);
  assert.match(api, /type TiaRequestOptions = \{ workspace\?: boolean; timeoutMs\?: number \}/);
  assert.match(api, /options\?\.timeoutMs \?\? REQUEST_TIMEOUT_MS/);
  assert.match(api, /createRequestSignal\(init\.signal, timeoutMs\)/);
  assert.match(api, /tiaRawRequest\(path, init, options\)/);
});

test("only historical preview gets 270 seconds; Apply and polling keep defaults", () => {
  assert.match(actions, /const HISTORICAL_PREVIEW_TIMEOUT_MS = 270_000;/);
  const previewStart = actions.indexOf("export async function previewHistoricalImportAction");
  const applyStart = actions.indexOf("export async function startHistoricalImportAction");
  const pollStart = actions.indexOf("export async function readHistoricalImportBatchAction");
  const preview = actions.slice(previewStart, applyStart);
  const apply = actions.slice(applyStart, pollStart);
  const polling = actions.slice(pollStart);
  assert.match(preview, /"\/clinic\/history\/preview"[\s\S]*?timeoutMs: HISTORICAL_PREVIEW_TIMEOUT_MS/);
  assert.doesNotMatch(apply, /timeoutMs/);
  assert.doesNotMatch(polling, /timeoutMs/);
  assert.equal((preview.match(/tiaRequest</g) ?? []).length, 1, "preview action must not add automatic retries");
});

test("setup page configures a 300-second duration for its Server Actions", () => {
  assert.match(setupPage, /export const maxDuration = 300;/);
  assert.doesNotMatch(setupPage, /^\s*"use client"/);
});

test("timeout aborts when caller does not provide a signal", async () => {
  const signal = createRequestSignal(undefined, 10);
  const reason = await waitForAbort(signal);
  assert.equal(reason.name, "TimeoutError");
});

test("caller AbortSignal cancels the request before the timeout", () => {
  const caller = new AbortController();
  const signal = createRequestSignal(caller.signal, 1_000);
  const reason = new DOMException("caller cancelled", "AbortError");
  caller.abort(reason);
  assert.equal(signal.aborted, true);
  assert.equal(signal.reason, reason);
});

test("custom timeout remains active when caller AbortSignal is also supplied", async () => {
  const caller = new AbortController();
  const signal = createRequestSignal(caller.signal, 10);
  const reason = await waitForAbort(signal);
  assert.equal(reason.name, "TimeoutError");
  assert.equal(caller.signal.aborted, false);
});

test("270-second preview signal keeps a response alive beyond the 15-second default", async () => {
  let requestCount = 0;
  const server = createServer((_request, response) => {
    requestCount += 1;
    setTimeout(() => {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ preview: "ready" }));
    }, 16_000);
  });
  server.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  const address = server.address();
  assert.ok(address && typeof address === "object");
  const startedAt = Date.now();

  try {
    const response = await fetch(`http://127.0.0.1:${address.port}`, {
      signal: createRequestSignal(undefined, 270_000),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { preview: "ready" });
    assert.ok(Date.now() - startedAt >= 15_000);
    assert.equal(requestCount, 1, "preview must not retry automatically");
  } finally {
    server.close();
  }
});
