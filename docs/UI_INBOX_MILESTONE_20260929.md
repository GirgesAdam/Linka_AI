# Linka Inbox / Conversations / Human Handoff milestone

Starting main SHA: `6dff620f935b7b5582fc30be6f8b4cd680602899`

## Product model

Inbox is an operational queue plus a conversation workspace. The redesign keeps separate axes separate: conversation status, ownership, active handoff, assignee, unread state, delivery state, priority, and channel health.

Primary scan hierarchy: needs attention -> ownership -> patient/latest message -> conversation state -> secondary metadata. Linka/Bot identity is shown only when AI is actually the owner or sender.

## Capability inventory

| Current capability | Current context | Business rule / dependency | Proposed UI location | User action path after redesign |
| --- | --- | --- | --- | --- |
| Search patient name / phone | Inbox list | Backend `q`, normalized phone search, max 120 chars | Queue toolbar | Inbox -> search -> conversation |
| Owner filter | Inbox list | `owner_type` is independently `ai` or `human` | Queue filter chips | Inbox -> Linka/team/all |
| Conversation status filter | Inbox list | open/pending/closed is not ownership | Queue filter chips | Inbox -> state filter |
| Assigned to me | Inbox list | Conversation assignee must equal current user | Attention filters | Inbox -> assigned to me |
| Unread only/count | List + workspace | Server unread count; opening marks read | Attention filter + patient row count | Inbox -> unread -> conversation |
| Pagination/live refresh | Inbox list | 50 rows/page; route refresh preserves server truth | Queue footer/background refresh | Inbox -> page; live updates stay current |
| Latest message/sender/time | Inbox list | Latest message is server-ranked by created time/id | Primary row preview | Scan row -> open |
| Channel identity | List + workspace | Conversation channel is independent of ownership | Secondary row/header metadata | Scan/open conversation |
| Active handoff + priority | List + workspace | Only pending/claimed handoff is active | Attention cue + ownership panel | Scan attention -> open -> act |
| Current assignee | List + workspace | Conversation/handoff assignment is explicit | Ownership cue + panel | Scan assignee -> open |
| Full message history | Conversation | API returns latest 500 ordered chronologically | Conversation hero | Open conversation -> scroll history |
| Patient/staff/Linka identity | Message bubble | `sender_type` is source of truth | Bubble sender line | Read message provenance |
| Text/media | Message body | Media proxy/provider metadata rules remain unchanged | Message stream | Open/render media in context |
| Delivery status/failure | Outbound message | Provider delivery lifecycle is received/queued/sent/delivered/read/failed/cancelled | Bubble metadata; failure alert | Read exact delivery truth |
| Mark read | Conversation open | Server action resets unread count | Automatic read marker | Open conversation |
| Latest-message auto-scroll | Conversation | Client scroll follows message count | Conversation stream | Open/new message -> latest |
| Closed conversation | Composer | Closed conversation cannot reply | Composer replacement state | See closed state; no false composer |
| Linka-owned conversation | Workspace | AI owner means staff reply requires takeover | Ownership strip | Open -> take over |
| Manual takeover | Workspace | Creates/reuses handoff, quiesces AI dispatch, claims to actor | Primary ownership action | Open -> take over -> reply |
| Unassigned handoff | Workspace | Pending handoff has no assignee | Composer action + ownership panel | Open -> claim -> reply |
| Assigned to me | Workspace | Claimed handoff assigned to current user | Composer enabled | Open -> reply |
| Assigned to another member | Workspace | Claim is blocked; admin may reassign | Read-only ownership state | Open -> see owner; admin can assign |
| Admin assignment | Ownership panel | Endpoint requires workspace admin; target must be active member | Ownership details | Open -> assign member |
| Handoff reason/category/priority/context | Workspace | Handoff fields remain independent; context may include latest customer message | Ownership details | Open -> inspect context |
| Resolve and return to Linka | Workspace | Requires assignee/admin; staff outbox must drain before AI ownership | Resolution action | Add note -> resolve -> Linka resumes |
| Resolve and close | Workspace | Resolution may set conversation closed; ownership returns to AI but conversation stays closed | Resolution option | Add note -> close option -> resolve |
| Resolution note | Workspace | Optional max 4000 chars | Resolution details | Resolve -> note |
| WhatsApp 24h freeform window | Composer | Freeform is valid only within 24h of latest inbound patient WhatsApp message | Composer gate | Open -> explanation -> approved follow-up |
| Approved follow-up | Composer | Requires active Meta Cloud route, active opted-in patient, verified identity, approved template | Blocked-reply CTA | Request follow-up -> queued/pending/unavailable truth |
| Template pending/unavailable | Composer | Meta approval/provider prerequisites determine state | Inline operational notice | Follow-up -> see next valid state |
| Channel degraded/reconnect/review | Inbox list | `provider_health` drives degraded, reconnect and account-review notices | Queue-level health notice | Inbox -> see provider issue before acting |
| Patient identity/profile | Workspace | Patient relation is canonical; phone may be absent | Compact context panel | Open -> patient profile |
| Draft preservation/idempotency | Composer | sessionStorage draft + idempotency key protects accidental duplicates | Composer | Type -> send/retry safely |

## Current -> proposed workflow

Current list already exposes the right filters but presents most metadata at similar visual weight. Proposed queue keeps all filters and pagination while making attention/ownership the scan anchors, patient + latest message the content anchor, and status/channel/time secondary.

Current workspace is chat card + desktop side rail. Proposed desktop keeps the conversation as the hero and a narrower operational ownership/context rail because assignment, handoff resolution and patient navigation are genuinely contextual. On tablet/mobile the conversation stays first; ownership/patient detail becomes compact/progressively disclosed rather than squeezing a desktop rail beside the chat.

## Ownership model

`owner_type=ai` means Linka currently owns the conversation. Staff takeover creates/reuses and claims a human handoff only after AI dispatch safety checks. A pending unassigned handoff needs a claimant. A claimed handoff has an explicit assignee. Another staff member cannot silently claim it. Admin can explicitly reassign. Resolving an open handoff returns ownership to Linka only after the staff outbox is drained; resolving with close keeps the conversation closed.

## Responsive direction

Desktop: operational queue; conversation hero + contextual ownership rail. Tablet: chronological conversation remains primary and context moves below/into compact details. Mobile: message history, ownership/attention state, next valid action and composer are the first screen priorities; patient/handoff secondary data must not shrink the chat.
