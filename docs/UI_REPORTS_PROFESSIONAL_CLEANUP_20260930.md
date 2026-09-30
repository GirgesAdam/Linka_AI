# Reports / Analytics professional cleanup

Starting `origin/main`: `01a14d0ab8d7fb28b83cc0e6a754797bb06b914f`

## Review finding

The existing Reports page was functionally sound but visually over-weighted the top KPI grid and treated one operational report — outstanding patient balances — as a special warning card outside the normal report system.

The page also still used legacy teal interaction styling in the report catalog and chart surfaces, and some report copy read like internal implementation language rather than polished product copy.

## Product direction

The page should answer, in order:

1. What happened in the selected period?
2. What are the few numbers worth looking at first?
3. What changed over time?
4. Which detailed report should I open next?

This is a reporting workspace, not a wall of equal-weight KPI cards.

## Changes

### Period summary
The six equal top StatCards were reduced to four decision-level summary metrics:

- net revenue
- recorded profit
- total appointments
- new patients

Gross payments and recorded expenses remain available as compact secondary context beneath the four primary metrics.

The standalone refunds card was removed. Refunds are still part of the authoritative backend profitability contract and therefore remain reflected in net revenue; no financial calculation was moved into the frontend.

### Detailed reports
The outstanding patient balances report now appears inside the normal Reports catalog instead of as a separate amber promotional/warning card above it.

Its underlying route and authoritative `/finance/outstanding-balances` contract are unchanged.

### Visual language
Scoped analytics interaction styling was migrated from legacy teal to the Linka accent tokens.

Semantic data meaning was not recolored or reinterpreted.

### Copy
Internal-sounding phrases were replaced with product-facing language:

- “ماذا يوضح؟”
- “طريقة الحساب”
- “لا توجد بيانات مطابقة للفترة”

The Reports catalog introduction now explains how to choose a report rather than describing implementation decisions such as “graphs and cards only”.

## Preserved contracts and capabilities

- month and full-year period selection
- revenue trend
- payment method breakdown
- appointment outcomes
- new-patient trend
- saved views
- report search
- detailed report filters
- CSV export
- saved customer cohorts
- campaign analytics link
- outstanding balances report
- all existing backend analytics/finance contracts

## Financial truth boundary

No totals are recomputed from unrelated frontend data.

The page continues to use:
- `/finance/profitability`
- `/finance/dashboard-trend`
- `/finance/payment-method-breakdown`
- `/analytics/catalog/run`

Refunds remain a backend-owned input to net revenue. They are simply no longer promoted as a standalone summary card.

## Runtime and verification

A temporary non-shipping preview harness was used to review the rebuilt hierarchy with synthetic report-shaped data at:

- 1440×900
- 768×1024
- 390×844

All three viewport runs returned HTTP 200, had zero page-level horizontal overflow, exposed no meaningful visible action target below 40px, showed no standalone refunds card, included the outstanding-balance report inside the normal report catalog, and did not contain the removed internal implementation copy.

Manual screenshot review confirmed the intended hierarchy and led to one additional responsive fix: analytics chart cards now allow their grid items to shrink on mobile, and the main line chart scales to the mobile viewport rather than opening with a horizontally clipped chart.

The preview route, temporary public-path exception, and browser-check scripts were removed before commit.

Local verification after the redesign:
- report UI contract: 4 passed
- relevant finance/analytics contracts: 38 passed
- frontend lint: 0 errors; only the 2 pre-existing Automations warnings
- Next type generation: passed
- TypeScript typecheck: passed
- production build: passed

Full GitHub CI remains the authoritative merge gate.
