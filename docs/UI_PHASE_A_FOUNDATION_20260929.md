# Linka UI Phase A — Foundation + Application Shell

Date: 2026-09-29
Starting `origin/main`: `1ff0f88637de242afd6394ea47de21dc9e2fb45f`
Branch: `agent/linka-ui-phase-a`

## Scope

This phase is intentionally narrow. It establishes the visual/application foundation needed by the core workflow redesigns without turning into a standalone design-system project.

Included:
- Inter + IBM Plex Sans Arabic through `next/font`.
- Semantic product/color tokens: navy identity, neutral workspace, restrained purple interaction accent, semantic status colors.
- Linka application shell identity.
- Navigation grouping based on receptionist and clinic-manager task models.
- Canonical frontend status-presentation contract.
- Basic mixed RTL/LTR utilities.
- Rebrand migration inventory only.

Explicitly excluded:
- Business lifecycle changes.
- Backend/API changes.
- Route or URL renames.
- Integration/environment identifier renames.
- Core workflow redesigns such as Appointments, Inbox, or Patient Workspace.

## Capability parity — application shell

| Current capability | Proposed UI location | User action path after Phase A |
| --- | --- | --- |
| Dashboard | `اليوم` group + mobile bottom nav | One click/tap, unchanged |
| Inbox + unread count | `اليوم` group + mobile bottom nav | One click/tap, unchanged |
| Appointments | `اليوم` group + mobile bottom nav | One click/tap, unchanged |
| Patients | `العملاء والمتابعة` + mobile bottom nav | One click/tap, unchanged |
| Tasks/follow-ups | `العملاء والمتابعة` | One click desktop; More → follow-ups mobile, unchanged depth |
| Doctors | `تشغيل العيادة` | One click desktop; More → clinic mobile, unchanged depth |
| Services/pricing (admin) | `تشغيل العيادة` | One click desktop; More → clinic mobile, unchanged depth |
| Inventory (admin) | `تشغيل العيادة` | One click desktop; More → clinic mobile, unchanged depth |
| Analytics | `الإدارة` | One click desktop; More → management mobile, unchanged depth |
| Finance | `الإدارة` | One click desktop; More → management mobile, unchanged depth |
| Automations/team/setup/activity (admin) | `الإدارة` | One click desktop; More → management mobile, unchanged depth |
| Workspace switch | Top application header/account sheet | Unchanged |
| Add clinic | Top application header/account sheet | Unchanged |
| Role/account identity | Top application header/account sheet | Unchanged |
| Logout | Sidebar/account sheet | Unchanged |
| Demo safety banner | Above page content | Unchanged |

No route was removed, hidden from an authorized role, or moved behind an additional desktop interaction.

## IA rationale

The grouping is a task-model hypothesis, not a backend-domain taxonomy.

Receptionist mental model:
1. What needs attention now? → Dashboard / Inbox / Appointments.
2. Who is the customer and what follow-up is needed? → Patients / Tasks.
3. What clinic resource/service is involved? → Doctors / Services / Inventory.

Clinic manager / owner mental model adds:
4. How is the clinic performing and configured? → Finance / Analytics / Automations / Team / Setup / Activity.

This keeps the high-frequency receptionist path at the top while preserving owner access without introducing role-specific navigation behavior in this phase.

## Canonical status presentation

Frontend status presentation now has an explicit contract:

`domain status → semantic intent → Arabic label + icon key + badge tone`

Domains currently covered: appointment, conversation, handoff, automation, patient, task, connection, message, priority, and generic fallback.

The mapping does not alter backend states or transitions. Existing `toneForStatus()` remains compatible and now derives from the canonical presentation layer. Core areas will adopt `StatusBadge` as each area is redesigned, beginning with Appointments.

## Rebrand migration inventory — no changes in Phase A

Visible frontend identity is already largely Linka; `frontend/src/lib/public-brand.ts` intentionally translates legacy public names to Linka.

Legacy/internal identifiers still exist and must not be renamed as visual cleanup:
- Environment/config keys including `TIA_API_URL`, `NEXT_PUBLIC_TIA_LEGAL_NAME`, `TIA_DEMO_ENABLED`, `NEXT_PUBLIC_TIA_DEMO_ENABLED`, `TIA_DEMO_EMAIL`, `TIA_DEMO_PASSWORD`, and E2E `TIA_*` keys.
- Backend module/file names such as `tia_customer_agent.py`, `tia_database.py`, and `tia_database_laser.py`.
- CI/evaluation scripts and fixtures that use `TIA_*` names.
- Deployment/runbook references and historical documentation.
- API/client compatibility identifiers and any external integration values that may be consumed outside the frontend.

Migration rule: visible identity can move to Linka independently; URLs, domains, callbacks, environment keys, integration identifiers, and compatibility names require a separate migration plan with consumers and rollback considered.

## Before → after summary

Before:
- Teal-led shell and bot mark made the application identity feel closer to an AI/admin dashboard.
- Navigation exposed a long feature list with only broad daily/admin separation.
- System typography used OS fallbacks.
- Status color logic existed but domain semantics were not represented by one frontend contract.

After Phase A:
- Deep navy anchors Linka identity while the workspace remains light and neutral.
- Purple is limited to interaction/selection; green/amber/red remain operational semantics.
- Linka shell uses a connection motif instead of treating the bot as the product identity.
- Navigation reflects daily work, patient follow-up, clinic operation, and management mental models without removing routes.
- Arabic typography and mixed-direction foundations are explicit.
- Status presentation has a canonical domain-aware frontend contract ready for core-area adoption.

## Verification

- `npm run lint`: pass with 0 errors; two pre-existing unused-import warnings remain in `automations/page.tsx` (`Clock3`, `Button`).
- `npm run typecheck`: pass.
- `npm run build`: pass on Next.js 16.3.4.
- Browser/dev verification used a temporary, uncommitted shell harness and was removed afterward.
- Desktop 1440×900: content rendered, no Next error overlay, no horizontal overflow, desktop sidebar visible.
- Tablet 768×1024: no horizontal overflow; desktop sidebar hidden; mobile navigation pattern active.
- Mobile 390×844: no horizontal overflow; bottom navigation active; More sheet opened and exposed all non-primary routes.
- Computed body font confirmed IBM Plex Sans Arabic with Inter fallback/Latin utility available.
- Temporary preview route, preview auth bypass, and screenshots were deleted before final status.

No dedicated Phase A unit test suite exists; verification therefore used the repository frontend gates plus browser interaction checks.
