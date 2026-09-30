# Linka Launch Evidence / Product Capture

Starting `origin/main`: `4fe71a6b72c7930831978d73eda0ce237aa403b2`

## Goal

Capture the shipping Linka product itself, using only the synthetic demo workspace, for landing pages, social content, sales outreach, demo calls, and product walkthroughs.

No marketing-only screens, invented capabilities, fake social proof, production patient data, or shipping auth bypasses are allowed.

## Canonical views

1. Dashboard — Needs Attention, Today, Coming Next.
2. Inbox — conversation ownership and Linka ↔ human handoff.
3. Appointments — daily operational schedule, free time, and booking context.
4. Patient Workspace — current operational state, timeline, packages, and device-specific pulse balances.
5. Appointment detail — primary service, add-ons, package/pulse choices, and payment context.
## Capture acceptance criteria

Each selected view must:
- come from the shipping product surface;
- use synthetic demo data only;
- show Linka rather than legacy Tia product branding;
- contain no debug/technical identifiers unsuitable for sales material;
- preserve semantic status colors;
- render mixed Arabic/English service/device names cleanly;
- have no layout break or horizontal overflow at the selected viewport;
- demonstrate a real workflow rather than a decorative state.

Captures are evidence, not new product design.

## Scoped UX corrections discovered before capture

A small product-correction pass was required before canonical capture:
- Appointment detail now exposes one unified edit surface instead of a separate reschedule CTA plus the unified editor.
- Current service/doctor choices remain representable even if that record was later deactivated, and the editor remounts from the saved appointment truth after a successful edit.
- Moving a non-laser appointment is rejected when it overlaps another active appointment in the same operational service category at the same branch.
- Laser scheduling keeps the existing device-resource semantics; Prime and Candela are independently scheduled resources.
- The Appointments board no longer has an “Other” lane; the service contract only supports laser, dermatology, and slimming operational categories, with laser split by device.
- The desktop working-hours banner above each schedule interval was removed.
- WhatsApp consent controls/badge were removed from Patient Profile presentation; underlying messaging/consent contracts were not deleted.
- The long explanatory sentence about patient active status was removed from the Patient List.
## Patient status semantics

`Patient.status` is an explicit CRM record field with allowed values `active`, `inactive`, and `blocked`.

New patient records default to `active`. “Active” therefore means the patient record is currently marked active in the CRM; it is not calculated from last-contact recency, appointment history, or a “last 3 months” rule.

## Product-truth boundaries

- Appointment financial context is shown only from existing payment/package/pulse contracts.
- Pulse balances remain device-specific.
- Handoff ownership remains sourced from the Inbox/handoff model.
- No revenue or balance totals are inferred for marketing screenshots.
- The demo dataset may be improved only through its synthetic fixture/reset path; production semantics and real clinic data are out of scope.

## Capture outputs

The final local evidence pack will contain canonical PNG captures plus a manifest recording:
- source main/merge SHA;
- route and workflow represented;
- viewport;
- demo-only data statement;
- any crop/use recommendation for Creative/Sales;
- known limitations.
