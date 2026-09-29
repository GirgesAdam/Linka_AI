# Linka Appointments — capability inventory and UX map

Baseline: `5451ede69f4b3f49f7d6a32c8b5c8f856ec00e1d`
Branch: `agent/linka-appointments`

## Product rule

Simplify the interface, not the product capability. Management surfaces configure domains; appointment surfaces keep contextual actions available at the point of care.

## Current workflow map

`Find/create patient → choose service/device → choose slot/doctor → optional package or pulse funding → create → confirm → visit → add services/products/packages/pulses → settle payment → complete/no-show/cancel/reschedule → history/refund/reconciliation`

The canonical lifecycle currently creates `pending` and `confirmed`; `checked_in` and `in_progress` are legacy-readable states that can still be closed but are no longer created by Linka. Completion/no-show become available after start time. Cancellation and reschedule are pre-start operations.

## Capability matrix

| Current capability | Current context | Business rule / dependency | Proposed UI location | User action path after redesign |
| --- | --- | --- | --- | --- |
| Standard booking | Daily schedule + manual form | Active branch/service/doctor; slot availability | Schedule primary action / empty desktop slot | Appointments → choose day/resource → empty slot / add appointment |
| Quick booking | Schedule free period / quick dialog | Can use quick scheduling path; still requires patient/service/doctor | Desktop free slot + mobile add action | Schedule → free time → quick booking |
| Confirm | Appointment detail | Pending only | Visit header primary action | Open appointment → Confirm |
| Complete | Appointment detail | After start; blocked by unresolved prepaid pulse settlement | Visit header primary action | Open appointment → Complete |
| No-show | Appointment detail | Available after start for open appointment | Visit overflow/secondary action | Open appointment → More → No-show |
| Cancel | Appointment detail | Pending/confirmed before start; notice window may require admin override | Destructive disclosure | Open appointment → More → Cancel → reason/override |
| Reschedule | Detail → reschedule page | Pending/confirmed before start; validates replacement slot and financial/package constraints | Visit header secondary action | Open appointment → Reschedule → slot/doctor → save |
| Legacy checked-in / in-progress closure | Appointment detail | Legacy rows remain readable; completion/no-show allowed after start | Same lifecycle actions | Open legacy appointment → Complete / No-show |
| Doctor scheduling | Booking/reschedule | Active doctor, service relationship and slot availability | Booking/reschedule form | Choose service/time → compatible doctor |
| Resource/category schedule | Daily schedule columns | Derived from laser device or service operational category | Desktop resource columns; mobile resource filter/label | Choose resources → inspect day |
| Laser device selection | Booking, service edit, add-on | Required for laser services; forbidden for non-laser services | Contextual service controls | Choose laser service → choose device |
| Device-specific service price | Booking/detail/add-on | Configured device price is authoritative for laser service | Service/device summary | Choose device → price follows configured device price |
| Device-aware duration/availability | Booking/reschedule | Availability uses service/device scheduling rules | Booking/reschedule form | Choose service/device → available times update |
| Change primary service | Appointment detail | Editable in pending/confirmed/legacy active states; financial/package/pulse backing can restrict changes | Visit service section | Open appointment → Edit service |
| Additional service | Appointment detail | Visit must be editable; cannot duplicate primary or existing add-on; doctor/service and currency rules apply | Visit charges → Add service | Open appointment → Visit charges → Add service |
| Additional laser service | Appointment detail | Requires device and configured device price | Add-service flow | Add service → device → billing choice |
| Pulse billing for add-on | Add-service flow | Supported modes are validated by pulse billing; usage/device/offer must match | Add-service billing choice | Add service → choose pulse option → save |
| Remove add-on | Appointment detail | Cannot directly remove package-backed or pulse-prepaid add-on until billing is corrected | Add-on row action | Visit charges → add-on → Remove |
| Buy package for primary service | Appointment detail | Active offer must match service and device; existing applied payment may require reconciliation first | Visit charges contextual package action | Open appointment → Package → choose offer → purchase/use |
| Use package session | Booking / appointment commerce | Package must belong to patient, be active/compatible; usage is reserved then consumed on completion | Booking funding choice + visit entitlement summary | Choose compatible package → book/visit |
| Remaining package sessions | Booking/detail | Derived from patient package ledger | Persistent entitlement summary near visit charges | Open appointment → see sessions remaining |
| Package for additional service | Add-on row | Offer must match add-on service/device; converts contextual visit line | Add-on row contextual action | Add-on → Package → choose offer |
| Remaining pulses | Booking/detail | Balance is device-specific | Persistent entitlement summary | Open appointment → see device pulse balance |
| Record laser pulses used | Appointment detail | Laser appointment only; drives settlement when pulse-backed | Service execution section | Open appointment → enter pulses → Save |
| Use existing pulses | Booking/checkout/add-on | Optional; balance is device-specific; mutually exclusive with session package at booking | Billing choice | Choose Use pulses when desired |
| Buy pulse pack | Checkout/add-on | Offer device must match and pack must satisfy selected settlement rules | Checkout contextual choice | Checkout → Buy pulse pack → offer |
| Pulse overage | Checkout | Device overage price configured; deficit calculated from usage vs balance | Checkout contextual choice | Checkout → Pay extra pulses |
| Normal payment instead of pulses | Checkout | `pulse_mode=none`; switching to pulse after money applied is restricted | Default checkout choice | Checkout → Pay normally |
| Appointment products | Appointment detail | Active inventory product; quantity and manual/unit pricing rules from inventory service | Visit charges → Products disclosure | Open appointment → Products → Add/remove |
| Payment recording | Appointment detail | Amount > 0 unless pulse resolution creates zero-money settlement; checkout revalidates balance | Sticky/visible checkout section | Open appointment → Checkout → amount/method → Record |
| Cash / Visa / InstaPay | Checkout | Canonical payment methods | Payment method segmented/select control | Checkout → choose method |
| Partial payment | Checkout | Amount can be below balance; ledger allocation updates remaining balance | Checkout | Enter partial amount → Record |
| Outstanding balance | Appointment detail | Computed from complete visit charge breakdown minus net paid | Financial summary, always visible | Open appointment → see due amount |
| Discount | Checkout/payment summary | Non-negative, affects amount due | Checkout advanced pricing | Checkout → discount |
| Refund | Payment transaction history | Only refundable payment amount; reason required | Payment history disclosure | Payments → transaction → Refund |
| Patient identity / phone | Appointment detail | Appointment patient is authoritative | Visit header/context rail | Open appointment → patient context |
| Patient record navigation | Schedule/detail | Existing patient route remains canonical workspace | Patient name link | Appointment → patient name |
| Appointment history | Detail side rail | Immutable status history | Context rail disclosure | Open appointment → History |
| Appointment automations | Detail side rail | Read-only operational jobs/status | Context rail disclosure | Open appointment → Automations |

## Desktop direction

Keep the resource-aware daily board because it exposes simultaneous clinic capacity. Make date/branch/resource controls the schedule toolbar, preserve clickable free periods for booking, and make appointment blocks scan as patient → service → status → time. No prices belong on the schedule.

The appointment itself is the operational workspace: identity/time/service first, lifecycle actions adjacent, then visit charges and checkout. Package, pulse, add-on and product actions stay contextual rather than being moved to management pages. History and automation diagnostics remain secondary context.

## Mobile direction

Do not render the desktop resource board horizontally. Render a chronological operational agenda with one row per appointment: `time → patient → service → status`, plus the resource/device/category as compact context. Existing resource filters still control the agenda. Tapping a row opens the same full appointment workspace.

## Progressive disclosure decisions

Cancellation, refunds, transaction history, automation jobs, status history, products, and less-common package conversion are appropriate disclosures because they are destructive, audit-oriented, or secondary. Add service, package/pulse entitlement, outstanding balance, payment, and lifecycle actions remain visible in the appointment context.

No capability is intentionally removed. The mobile agenda reduces schedule density but not action reach: all visit actions remain one appointment tap away.

## Click-impact review

Core paths do not gain a material step. Desktop booking from a free resource period remains direct. Opening an appointment remains one tap/click. Visit commerce remains inside the appointment. Destructive/audit actions may require opening a disclosure, matching their current safety-oriented interaction.

## Backend requirements

No new backend behavior is required for this milestone. The UI consumes existing allowed-actions, availability, device pricing, package, pulse settlement, inventory, and payment contracts. A future product decision would be required to reintroduce `checked_in` / `in_progress` as newly-created lifecycle steps; this redesign does not do that.

## Implementation notes

The schedule now has two responsive representations over the same appointment data and filters: desktop resource board at `md+`, and a true chronological mobile agenda below `md`. This avoids horizontal calendar scrolling without creating a second business workflow.
