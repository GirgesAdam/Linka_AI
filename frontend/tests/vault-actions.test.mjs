import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import Module, { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const req = createRequire(resolve(root, "package.json"));
const ts = req("typescript");
const React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
const UUID = "11111111-1111-4111-8111-111111111111";
const BRANCH = "22222222-2222-4222-8222-222222222222";
const PENDING = "33333333-3333-4333-8333-333333333333";

function compileSource(relative, mocked) {
  const filename = resolve(root, relative);
  const source = readFileSync(filename, "utf8");
  const transpiled = ts.transpileModule(source, {
    fileName: filename,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  }).outputText;
  const mod = new Module(filename);
  mod.filename = filename;
  mod.paths = Module._nodeModulePaths(dirname(filename));
  const native = mod.require.bind(mod);
  mod.require = (name) => Object.hasOwn(mocked, name) ? mocked[name] : native(name);
  mod._compile(transpiled, filename);
  return mod.exports;
}

function actionHarness() {
  const requests = [];
  const validations = [];
  const actions = compileSource("src/app/(dashboard)/vault-actions.ts", {
    "next/cache": { revalidatePath: (path) => validations.push(path) },
    "@/lib/action-feedback": {
      actionFeedbackFailure: (err, fallback) => ({
        error: err instanceof Error ? err.message : fallback, notice: null,
      }),
    },
    "@/lib/tia/api": {
      tiaRequest: async (path, opts) => {
        requests.push({ path, method: opts.method, body: JSON.parse(opts.body) });
        return {};
      },
    },
  });
  return { actions, requests, validations };
}

function data(fields) {
  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) form.set(key, String(value));
  return form;
}

test("reception full-balance transfer sends one idempotent request and refreshes both views", async () => {
  const h = actionHarness();
  const result = await h.actions.transferReceptionBalance(
    { notice: null, error: null },
    data({ branch_id: BRANCH, idempotency_key: UUID }),
  );
  assert.match(result.notice, /تم توريد العهدة/);
  assert.deepEqual(h.requests, [{
    path: "/vault/reception/" + BRANCH + "/transfer",
    method: "POST",
    body: { idempotency_key: UUID },
  }]);
  assert.deepEqual(h.validations, ["/dashboard", "/finance"]);
});

test("transfer without a valid idempotency key has zero network side effects", async () => {
  const h = actionHarness();
  const result = await h.actions.transferReceptionBalance(
    { notice: null, error: null },
    data({ branch_id: BRANCH, idempotency_key: "" }),
  );
  assert.ok(result.error);
  assert.equal(h.requests.length, 0);
});

test("expense requires description and parses decimal pounds into integer piasters", async () => {
  const h = actionHarness();
  const result = await h.actions.recordReceptionExpense(
    { notice: null, error: null },
    data({ branch_id: BRANCH, idempotency_key: UUID, amount: "10.05", description: "Supplies" }),
  );
  assert.ok(result.notice);
  assert.equal(h.requests[0].body.amount_minor, 1005);
  assert.equal(h.requests[0].body.description, "Supplies");
  assert.equal(h.requests[0].body.idempotency_key, UUID);
  const h2 = actionHarness();
  const reject = await h2.actions.recordReceptionExpense(
    { notice: null, error: null },
    data({ branch_id: BRANCH, idempotency_key: UUID, amount: "-5", description: "" }),
  );
  assert.ok(reject.error);
  assert.equal(h2.requests.length, 0);
});

test("admin pending-resolution form submits branch and evidence but never silently chooses branch", async () => {
  const h = actionHarness();
  const result = await h.actions.resolvePendingVaultPayment(
    { notice: null, error: null },
    data({ pending_id: PENDING, branch_id: BRANCH, explanation: "Verified allocation evidence" }),
  );
  assert.match(result.notice, /تمت مراجعة الدفعة/);
  assert.equal(h.requests[0].path, "/vault/pending-payments/" + PENDING + "/resolve");
  assert.deepEqual(h.requests[0].body, {
    branch_id: BRANCH, explanation: "Verified allocation evidence",
  });
});

test("resolution with missing audit explanation is rejected before API call", async () => {
  const h = actionHarness();
  const result = await h.actions.resolvePendingVaultPayment(
    { notice: null, error: null },
    data({ pending_id: PENDING, branch_id: BRANCH, explanation: "short" }),
  );
  assert.ok(result.error);
  assert.equal(h.requests.length, 0);
});

test("zero opening balance activation is accepted as an explicit one-time action", async () => {
  const h = actionHarness();
  const result = await h.actions.activateVaultAccount(
    { notice: null, error: null },
    data({ kind: "reception", branch_id: BRANCH, opening_balance: "0" }),
  );
  assert.ok(result.notice);
  assert.deepEqual(h.requests[0].body, {
    kind: "reception", branch_id: BRANCH, opening_balance_minor: 0,
    currency: "EGP",
  });
});

const displayStub = (tag) => function Stub({ children, ...props }) {
  const stripped = Object.fromEntries(
    Object.entries(props).filter(([k]) => !k.startsWith("on") && !["action", "confirmMessage", "submitLabel", "pendingLabel", "refreshAfterSuccess"].includes(k)),
  );
  const submit = props.submitLabel
    ? React.createElement("button", { type: "submit" }, props.submitLabel)
    : null;
  return submit
    ? React.createElement(tag, stripped, children, submit)
    : React.createElement(tag, stripped, children);
};

const ElectronicPendingActions = compileSource(
  "src/app/(dashboard)/finance/electronic-pending-actions.tsx",
  {
    "@/components/vault-action-form": { VaultActionForm: displayStub("form") },
    "../vault-actions": {
      reviewElectronicOpeningReceipt: async () => ({ notice: "reviewed" }),
      retryEligibleElectronicReceipt: async () => ({ notice: "posted" }),
    },
  },
).ElectronicPendingActions;

const PendingPayments = compileSource(
  "src/app/(dashboard)/finance/pending-vault-payments.tsx",
  {
    "lucide-react": { AlertTriangle: () => React.createElement("i"), ShieldCheck: () => React.createElement("i") },
    "@/components/action-feedback-form": { ActionFeedbackForm: displayStub("form") },
    "@/components/ui/select": { Select: displayStub("select") },
    "@/components/ui/textarea": { Textarea: displayStub("textarea") },
    "@/lib/format": { formatMoney: (minor, currency) => `${minor / 100} ${currency}` },
    "../vault-actions": { resolvePendingVaultPayment: async () => ({ notice: "ok", error: null }) },
    "./electronic-pending-actions": { ElectronicPendingActions },
  },
).PendingVaultPayments;

test("pending payment read errors never display a false empty state", () => {
  const html = renderToStaticMarkup(
    React.createElement(PendingPayments, { payments: [], vaults: [], unavailable: true }),
  );
  assert.match(html, /تعذر تحميل الحركات المالية المعلقة/);
  assert.doesNotMatch(html, /لا توجد حركات معلقة/);
});

test("unresolved multi-branch evidence is displayed but posting form is withheld", () => {
  const payment = {
    pending_id: PENDING, transaction_id: UUID, amount_minor: 15000,
    currency: "EGP", payment_method: "cash", transaction_type: "payment",
    source: "staff", created_at: new Date().toISOString(),
    reason: "ambiguous_branch", external_reference: null,
    appointment_id: null, allocations: [],
    resolution_eligible: false, verified_branch_id: null,
  };
  const html = renderToStaticMarkup(
    React.createElement(PendingPayments, {
      payments: [payment], vaults: [{ branch_id: BRANCH, branch_name: "Clinic", activated: true }],
      unavailable: false,
    }),
  );
  assert.match(html, /لا يمكن الترحيل من هذه الشاشة/);
  assert.doesNotMatch(html, /اعتماد وترحيل الدفعة/);
  assert.match(html, new RegExp(UUID));
});

test("eligible reviewed cash payment renders audited resolution form", () => {
  const payment = {
    pending_id: PENDING, transaction_id: UUID, amount_minor: 15000,
    currency: "EGP", payment_method: "cash", transaction_type: "payment",
    source: "staff", created_at: new Date().toISOString(),
    reason: "incomplete_payment_allocations", external_reference: null,
    appointment_id: UUID, allocations: [{
      allocation_id: UUID, appointment_id: UUID, branch_id: BRANCH,
      branch_name: "Clinic", amount_minor: 15000, patient_matches_source: true,
    }],
    resolution_eligible: true, verified_branch_id: BRANCH,
  };
  const html = renderToStaticMarkup(
    React.createElement(PendingPayments, {
      payments: [payment], vaults: [{ branch_id: BRANCH, branch_name: "Clinic", activated: true }],
      unavailable: false,
    }),
  );
  assert.match(html, /اعتماد وترحيل الدفعة/);
  assert.match(html, /name="explanation"/);
  assert.match(html, /name="branch_id"/);
});


test("electronic opening review submits explicit attestation without a ledger amount", async () => {
  const h = actionHarness();
  const result = await h.actions.reviewElectronicOpeningReceipt(
    { notice: null, error: null },
    data({
      pending_id: PENDING, outcome: "included_in_opening",
      explanation: "Bank statement matched to declared opening",
    }),
  );
  assert.ok(result.notice);
  assert.deepEqual(h.requests, [{
    path: "/vault/pending-payments/" + PENDING + "/electronic-opening-review",
    method: "POST",
    body: {
      outcome: "included_in_opening",
      explanation: "Bank statement matched to declared opening",
    },
  }]);
});

test("opening review rejects unknown outcome before contacting backend", async () => {
  const h = actionHarness();
  const result = await h.actions.reviewElectronicOpeningReceipt(
    { notice: null, error: null },
    data({ pending_id: PENDING, outcome: "auto_post", explanation: "Mistaken auto-post" }),
  );
  assert.ok(result.error);
  assert.equal(h.requests.length, 0);
});

test("eligible postactivation electronic retry sends explicit audited operation", async () => {
  const h = actionHarness();
  const result = await h.actions.retryEligibleElectronicReceipt(
    { notice: null, error: null },
    data({ pending_id: PENDING, explanation: "Confirmed the receipt after channel activation" }),
  );
  assert.ok(result.notice);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].path, "/vault/pending-payments/" + PENDING + "/electronic-retry");
  assert.equal(h.requests[0].body.explanation, "Confirmed the receipt after channel activation");
});

function electronicPayment(overrides = {}) {
  return {
    pending_id: PENDING, transaction_id: UUID, amount_minor: 12500,
    currency: "EGP", payment_method: "visa", transaction_type: "payment",
    source: "staff", created_at: new Date().toISOString(),
    reason: "main_channel_not_activated", external_reference: null,
    appointment_id: null, allocations: [],
    resolution_eligible: false, verified_branch_id: null,
    electronic_action: null, review_outcome: null, review_note: null, reviewed_at: null,
    ...overrides,
  };
}

test("unactivated electronic channel displays warning without posting form", () => {
  const html = renderToStaticMarkup(React.createElement(
    ElectronicPendingActions, { payment: electronicPayment() },
  ));
  assert.match(html, /فعّل القناة أولًا/);
  assert.doesNotMatch(html, /<form/);
});

test("preactivation electronic receipt requires explicit opening reconciliation choice", () => {
  const html = renderToStaticMarkup(React.createElement(
    ElectronicPendingActions,
    { payment: electronicPayment({ electronic_action: "opening_review" }) },
  ));
  assert.match(html, /name="outcome"/);
  assert.match(html, /included_in_opening/);
  assert.match(html, /requires_accounting_decision/);
  assert.match(html, /name="explanation"/);
});

test("eligible postactivation electronic receipt provides reviewed retry action", () => {
  const html = renderToStaticMarkup(React.createElement(
    ElectronicPendingActions,
    { payment: electronicPayment({ electronic_action: "eligible_retry" }) },
  ));
  assert.match(html, /اعتماد إعادة الترحيل/);
  assert.match(html, /name="explanation"/);
});

test("reviewed preactivation receipt remains visible but is not offered for reposting", () => {
  const html = renderToStaticMarkup(React.createElement(
    ElectronicPendingActions,
    { payment: electronicPayment({
      review_outcome: "included_in_opening",
      review_note: "Bank opening reconciliation",
      reviewed_at: new Date().toISOString(),
    }) },
  ));
  assert.match(html, /Bank opening reconciliation/);
  assert.doesNotMatch(html, /<form/);
});


test("Demo Finance renders the vault policy explanation and no activation controls", () => {
  const StubIcon = () => React.createElement("i");
  const { MainTreasurySection } = compileSource(
    "src/app/(dashboard)/finance/main-treasury-section.tsx",
    {
      "lucide-react": Object.fromEntries(
        ["Banknote", "Building2", "CreditCard", "Receipt", "ShieldCheck", "WalletCards"]
          .map((name) => [name, StubIcon]),
      ),
      "@/components/action-feedback-form": { ActionFeedbackForm: displayStub("form") },
      "@/components/vault-action-form": { VaultActionForm: displayStub("form") },
      "@/components/ui/card": {
        Card: displayStub("section"), CardHeader: displayStub("div"),
        CardTitle: displayStub("h3"), CardContent: displayStub("div"),
      },
      "@/components/ui/input": { Input: displayStub("input") },
      "@/components/ui/select": { Select: displayStub("select") },
      "@/lib/format": { formatMoney: (amount, currency) => `${amount} ${currency}` },
      "./pending-vault-payments": {
        PendingVaultPayments: () => React.createElement("div"),
      },
      "../vault-actions": {
        activateVaultAccount: () => ({}),
        setReceptionBranchGrant: () => ({}),
      },
    },
  );
  const props = {
    treasury: {
      cash_minor: 0, visa_minor: 0, instapay_minor: 0, currency: "EGP",
      total_recorded_minor: 0, transfers_today_minor: 0,
      active_channels: [], unattributed_payment_count: 0,
    },
    vaults: [{
      branch_id: BRANCH, branch_name: "Synthetic Branch", activated: false,
      balance_minor: 0, currency: "EGP",
    }],
    members: [], grants: [], movements: [], pendingPayments: [],
    pendingUnavailable: false, filters: {},
  };
  const demo = renderToStaticMarkup(React.createElement(
    MainTreasurySection, { ...props, isDemo: true },
  ));
  assert.match(demo, /الخزنة غير متاحة في الوضع التجريبي حالياً/);
  assert.doesNotMatch(demo, /name="opening_balance"/);
  assert.doesNotMatch(demo, /name="kind"/);

  const real = renderToStaticMarkup(React.createElement(
    MainTreasurySection, { ...props, isDemo: false },
  ));
  assert.match(real, /name="opening_balance"/);
  assert.match(real, /name="kind"/);
});

test("Demo Reception remains visible with a vault-disabled explanation", () => {
  const { ReceptionVaultSection } = compileSource(
    "src/app/(dashboard)/dashboard/reception-vault-section.tsx",
    {
      "lucide-react": {
        Banknote: () => React.createElement("i"),
        ArrowLeftRight: () => React.createElement("i"),
        Receipt: () => React.createElement("i"),
      },
      "@/components/vault-action-form": { VaultActionForm: displayStub("form") },
      "@/components/ui/card": {
        Card: displayStub("section"), CardHeader: displayStub("div"),
        CardTitle: displayStub("h3"), CardContent: displayStub("div"),
      },
      "@/components/ui/input": { Input: displayStub("input") },
      "@/components/ui/textarea": { Textarea: displayStub("textarea") },
      "@/lib/format": { formatMoney: (amount, currency) => `${amount} ${currency}` },
      "../vault-actions": {
        recordReceptionExpense: () => ({}), transferReceptionBalance: () => ({}),
      },
    },
  );
  const html = renderToStaticMarkup(React.createElement(
    ReceptionVaultSection, { vaults: [], unavailable: false, isDemo: true },
  ));
  assert.match(html, /الخزنة غير متاحة في الوضع التجريبي حالياً/);
  assert.match(html, /خزنة الريسبشن/);
  assert.doesNotMatch(html, /name="opening_balance"/);
});
