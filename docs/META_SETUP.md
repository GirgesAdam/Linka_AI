# Linka Meta Transport — App Setup & Readiness

Status date: 2026-10-09
Owner: Engineer 1 — Meta Transport & Connectivity

## Scope and dependency gate

This document covers E1-T0 only: Meta Developer App readiness, permissions, OAuth/webhook configuration, production restrictions, token lifecycle, and the environment contract needed by Linka.

Do not implement or migrate shared social identity models until Engineer 2's **Social Channel Identity Foundation** is merged into `main`. As of the starting commit for this task (`be21b78b6e38ff00b050a91a5e5dcc04d0609f2d`), no merged PR or branch with that Foundation name was found.

Current Linka facts that must be preserved:

- `ChannelConnection` already supports `facebook` and `instagram` channel values.
- provider credentials already have encrypted-at-rest storage through `ChannelProviderCredential` and `CHANNEL_CREDENTIAL_ENCRYPTION_KEY`.
- `ChannelIdentity` currently requires a `patient_id`; E1 must not change that contract before Engineer 2's Foundation lands.
- `META_GRAPH_API_VERSION` is currently pinned to `v25.0`. Meta released Graph API v26.0 on 2026-07-29, while v25.0 remains supported until 2028-07-29. Do not silently upgrade the shared version as part of Meta social onboarding; version upgrade needs its own compatibility gate, especially because WhatsApp already uses the same setting.

## Selected Meta integration path

Primary Linka path:

1. Meta **Business** app owned by Linka's verified Business Portfolio.
2. **Facebook Login for Business** for OAuth.
3. Discover manageable Pages via Graph API.
4. Connect a Facebook Page for Messenger.
5. Discover the Page-linked Instagram Professional Account (`instagram_business_account`) for Instagram messaging.
6. Use a single Meta webhook gateway in Linka and resolve every event by external account -> `ChannelConnection` -> workspace.

This keeps Messenger and Instagram under one connection experience and matches the planned `Settings -> Connect Meta -> OAuth -> account discovery -> ChannelConnection` flow.

Instagram API with Instagram Login is intentionally not the E1 baseline. It is a valid Meta path and no longer requires a linked Facebook Page, but adopting two different OAuth/token models in the first omnichannel implementation would widen T1-T9 unnecessarily. It can be evaluated later as a separate compatibility path.

## Meta App dashboard checklist

### A. Create / own the app

- [ ] Create a Meta app for Linka with the Business app/use-case path available in the current dashboard.
- [ ] Attach it to Linka's verified Business Portfolio.
- [ ] Keep the app in Development mode during initial integration testing.
- [ ] Add the production app domain: `linkaai.online`.
- [ ] Configure Privacy Policy URL: `https://app.linkaai.online/privacy`.
- [ ] Configure Data Deletion URL/instructions: `https://app.linkaai.online/data-deletion`.
- [ ] Add a monitored developer/contact email.
- [ ] Do not copy App Secret, access tokens, or verify tokens into Git, screenshots, tickets, or frontend configuration.

### B. Facebook Login for Business / OAuth

- [ ] Add **Facebook Login for Business**.
- [ ] Enable Client OAuth Login and Web OAuth Login.
- [ ] Keep HTTPS and strict redirect matching enabled.
- [ ] Add the exact Linka callback from `META_OAUTH_REDIRECT_URI` to **Valid OAuth Redirect URIs**.
- [ ] Create a Login for Business configuration if the current Meta dashboard requires a configuration ID and store that non-secret ID as `META_FACEBOOK_LOGIN_CONFIG_ID`.
- [ ] Do not use wildcard redirects.

Recommended backend route for T1:

```text
GET  /api/v1/channels/meta/oauth/start
GET  /api/v1/channels/meta/oauth/callback
```

The deployed absolute callback URL must be decided from the real backend public origin in T1 and then copied exactly into Meta. Do not guess or hardcode a localhost callback in production.

### C. Messenger

- [ ] Add/customize the Messenger messaging use case/product.
- [ ] Connect a dedicated Linka test Facebook Page.
- [ ] Enable Page webhook subscriptions required by the implementation.
- [ ] Minimum inbound field: `messages`.
- [ ] Add `messaging_postbacks` when interactive messages are supported.
- [ ] Add delivery/read/echo fields only when E1-T7 consumes them (for example `message_deliveries`, `message_reads`, `message_echoes`).

### D. Instagram messaging

- [ ] Create/use a dedicated Instagram Professional account (Business or Creator) for testing.
- [ ] Link that account to the dedicated test Facebook Page for the selected Facebook Login path.
- [ ] Confirm the test Meta user has the required Page messaging task/access.
- [ ] Enable Instagram messaging/webhook configuration for the app.
- [ ] Minimum inbound field: `messages`.
- [ ] Add `messaging_postbacks`, reactions, seen/read, referral, or edit fields only when Linka has code that handles them.
- [ ] Confirm Instagram's setting that allows connected tools/API access to messages is enabled when Meta exposes that control.

### E. Webhook callback

Planned callback:

```text
GET  /api/v1/webhooks/meta   # verification challenge
POST /api/v1/webhooks/meta   # Messenger + Instagram events
```

- [ ] Add the exact deployed callback URL from `META_WEBHOOK_CALLBACK_URL` in the Meta app.
- [ ] Configure the random verify token from `META_WEBHOOK_VERIFY_TOKEN`.
- [ ] Verification GET must compare the verify token and return Meta's challenge only on exact match.
- [ ] POST must verify `X-Hub-Signature-256` using the raw request bytes and `META_APP_SECRET` with HMAC-SHA256.
- [ ] Compare signatures with constant-time comparison.
- [ ] Reject invalid/missing signatures before parsing or routing business data.
- [ ] Acknowledge valid events promptly; durable processing/dedupe must tolerate provider retries.

### F. Test users and assets

- [ ] Linka Meta app administrator/developer account.
- [ ] At least one separate Meta test user/tester accepted into the app.
- [ ] Dedicated test Facebook Page.
- [ ] Dedicated Instagram Professional account linked to the test Page.
- [ ] At least one sender account capable of messaging the test Page/Instagram account.
- [ ] Record which identities are app-role/test identities so Development-mode success is not confused with production readiness.

## Permissions — planned minimum

| Permission / feature | Linka use | Baseline |
| --- | --- | --- |
| `pages_show_list` | Discover Pages the authorized person can manage | Required |
| `pages_manage_metadata` | Subscribe/manage Page webhook subscriptions and Messenger metadata required by the messaging integration | Required |
| `pages_messaging` | Receive/send Messenger Page conversations | Required |
| `pages_read_engagement` | Messenger conversation/history surfaces that require Page engagement read access | Required for conversation sync/history path; keep out if T1 proves unnecessary |
| `instagram_basic` | Discover/read the linked Instagram Professional account on the Facebook Login path | Required |
| `instagram_manage_messages` | Read/respond to Instagram Direct messages | Required |
| `public_profile` | Base Facebook Login identity granted with login | Platform baseline |
| Human Agent feature/tag | Human-only replies outside the standard 24-hour window, where Meta permits it | Optional; requires explicit approval before use |
| `business_management` | Business Portfolio asset APIs | **Not requested by default**; add only if `/me/accounts` cannot satisfy discovery |

Do not request publishing, ads, insights, comments, leads, or other permissions unless a Linka feature actually uses them.

## App Review / Advanced Access / production gate

Development mode / Standard Access is for app-role/test identities. It is not proof that arbitrary clinic customers can use Messenger or Instagram through Linka.

Production readiness requires all of the following before onboarding real clinic assets:

- [ ] Linka Business Portfolio verification completed.
- [ ] Meta app switched to Live only after the required review gates are complete.
- [ ] Advanced Access granted for the permissions/features Meta marks as required for non-role users.
- [ ] `pages_messaging` review demonstrates a real user-initiated Messenger conversation and Linka reply.
- [ ] `instagram_manage_messages` review demonstrates the Instagram professional-account connection and real DM receive/reply path.
- [ ] Reviewer instructions include a working Linka test account and deterministic navigation to Settings -> Meta connection -> Inbox.
- [ ] Screencast includes OAuth grant, asset selection, webhook-driven inbound message, and a visible human reply delivered back to the customer.
- [ ] Data handling questions describe encrypted credential storage, tenant isolation, deletion/disconnect behavior, and no sale of conversation data.
- [ ] Re-test from a Meta account with **no app role** after approval; internal role accounts can hide missing Advanced Access.

Meta's current Conversations API guidance states that Advanced Access is required for conversations with people who do not have a role on the app/business assets, and that Instagram Messaging on the Facebook Login path requires the app to be owned by a verified business.

## Messaging windows and policy readiness

E1-T6 must enforce provider policy server-side. The Agent and UI must not decide whether a Meta send is legal.

Baseline policy model for Messenger and Instagram messaging:

```text
last customer inbound <= 24h
  -> standard reply allowed

24h < last customer inbound <= 7d
  -> human-agent reply only when the Meta Human Agent feature/tag is approved and the message is genuinely human-sent

> 7d (or no user-initiated conversation)
  -> block normal outbound; wait for customer re-entry or another explicitly supported Meta mechanism
```

Do not treat successful provider calls from app admins/testers as policy proof. E1-T6 must return normalized outcomes such as `outside_messaging_window`, `human_agent_only`, or `reconnect_required` rather than leak Meta-specific errors through the business layer.

## Token lifecycle and reconnect strategy

For the selected Facebook Login path:

1. OAuth callback receives a short-lived user token/code flow result server-side.
2. Exchange to the appropriate long-lived user token where supported.
3. Discover Pages with `/me/accounts` (or the current equivalent) and obtain the Page access token for the selected Page.
4. Discover `instagram_business_account` from the Page for Instagram.
5. Store provider access tokens only in encrypted `ChannelProviderCredential` fields.
6. Store token expiry metadata when Meta supplies it; never infer a permanent lifetime.
7. Health-check with token introspection/provider probe and mark connection reconnect-required when invalid/revoked/expired.
8. Disconnect must unsubscribe Linka where appropriate, revoke/remove local credentials, and set connection state accurately.

Page access tokens are long-lived but can still become invalid because permissions, Page/business access, user security state, app state, or provider policy changes. E1-T8 must therefore model reconnect explicitly rather than displaying a permanently optimistic `Connected` state.

## Webhook reliability/security requirements carried into E1-T2/E1-T9

- Verify `X-Hub-Signature-256` against raw bytes before JSON trust.
- Resolve tenant only from the destination external account and an existing `ChannelConnection`.
- Never derive workspace from sender-provided identity.
- Dedupe provider events; Meta retries failed webhook deliveries.
- Make replay safe and idempotent.
- Accept out-of-order delivery without corrupting conversation/message state.
- Redact access tokens, App Secret, authorization code, state secret, and verify token from logs/errors/traces.
- Log stable non-secret provider IDs needed for diagnosis.
- Keep provider request/response logging bounded and redact Authorization/query tokens.
- If Linka later enables Meta webhook mTLS verification, account for Meta's 2026 CA transition rather than pinning the retired CA.

## Required environment variables

Platform-level variables (server only):

```dotenv
# Existing shared Meta Graph version. Keep v25.0 for this project until a separate version-upgrade gate approves v26.
META_GRAPH_API_VERSION=v25.0

# Linka Meta Business App
META_APP_ID=REPLACE_ME
META_APP_SECRET=REPLACE_ME

# Exact deployed URLs configured in Meta App Dashboard
META_OAUTH_REDIRECT_URI=https://YOUR_BACKEND_ORIGIN/api/v1/channels/meta/oauth/callback
META_WEBHOOK_CALLBACK_URL=https://YOUR_BACKEND_ORIGIN/api/v1/webhooks/meta

# Random server-side values; never NEXT_PUBLIC_*
META_WEBHOOK_VERIFY_TOKEN=REPLACE_WITH_RANDOM_SECRET
META_OAUTH_STATE_SIGNING_KEY=REPLACE_WITH_RANDOM_SECRET

# Non-secret; only required if Facebook Login for Business uses a configuration ID
META_FACEBOOK_LOGIN_CONFIG_ID=REPLACE_ME

# Existing Linka encrypted provider credential storage
CHANNEL_CREDENTIAL_ENCRYPTION_KEY=REPLACE_WITH_FERNET_KEY
```

Per-clinic Page/Instagram access tokens must **not** be environment variables. They belong in encrypted provider credential storage bound to the clinic's `ChannelConnection` after T1.

Frontend must never receive `META_APP_SECRET`, access tokens, `META_WEBHOOK_VERIFY_TOKEN`, `META_OAUTH_STATE_SIGNING_KEY`, or decrypted provider credentials.

## E1-T0 completion checklist

Repository/readiness work:

- [x] Fresh main recorded.
- [x] Isolated worktree + short-lived branch.
- [x] Existing Linka channel credential architecture reviewed.
- [x] Shared-model Foundation dependency identified and respected.
- [x] OAuth architecture selected.
- [x] Minimum permissions documented.
- [x] App Review / Advanced Access / business verification documented.
- [x] Messaging-window policy documented.
- [x] Token lifecycle and reconnect strategy documented.
- [x] Webhook verification/signature/retry requirements documented.
- [x] Required environment-variable contract documented without secrets.

Meta Dashboard actions (must be completed in the Meta Developer account, not in Git):

- [ ] Meta Business App created/selected.
- [ ] Linka verified Business Portfolio attached.
- [ ] Facebook Login for Business configured.
- [ ] Messenger enabled.
- [ ] Instagram messaging enabled.
- [ ] Production/staging OAuth redirect URI entered.
- [ ] `/webhooks/meta` callback + verify token configured after E1-T2 exists.
- [ ] Dedicated test Page created/selected.
- [ ] Dedicated Instagram Professional account created/selected and linked to test Page.
- [ ] Test user/tester roles accepted.
- [ ] Standard-access internal E2E executed.
- [ ] App Review package prepared/submitted when T1-T5 provide a complete demoable flow.
- [ ] Advanced Access + Live-mode external-user test passed.

## Sources reviewed for this readiness contract

Meta-maintained API collections and Meta developer references were checked on 2026-10-09, including:

- Messenger Platform API: https://www.postman.com/meta/messenger-platform-api/
- Instagram API: https://www.postman.com/meta/instagram/
- Meta Graph API version lifecycle: https://developers.facebook.com/docs/graph-api/changelog/versions/
- Meta Graph API Webhooks: https://developers.facebook.com/docs/graph-api/webhooks/
- Meta Permissions Reference: https://developers.facebook.com/docs/permissions/reference/
- Meta Access Levels: https://developers.facebook.com/docs/graph-api/overview/access-levels/

Meta dashboard labels and permission dependencies can change independently of code. Before App Review submission, re-check the live Meta dashboard and current permission reference rather than relying only on this file.