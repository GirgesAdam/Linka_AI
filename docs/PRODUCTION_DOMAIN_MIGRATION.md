# Linka production domain migration

Canonical product domains for the next production release:

- Root marketing domain: `https://linkaai.online`
- Application domain: `https://app.linkaai.online`

The previous application host, `https://app.tiaai.online`, is compatibility-only during migration. Do not remove it from Supabase redirect allow-lists or backend CORS until the new host and all auth flows are verified in production.

## Repository audit inventory

Pre-change old-domain references were found in five categories:

- Current documentation: README and recruiter demo application URLs.
- Current demo identity: `demo@tiaai.online` in README/recruiter demo. This is an existing external auth identity, not a sender-domain reference; keep it until that auth user is deliberately migrated.
- Synthetic seed/example data: branch/staff addresses in `backend/scripts/seed_realistic_aesthetic_clinic.py`; these move to `@linkaai.online`.
- Runtime Auth: no hardcoded old production hostname existed; signup/recovery callbacks were derived from the request origin.
- Runtime backend/CORS: no hardcoded old production hostname existed; production CORS is supplied through `CORS_ORIGINS`.

No historical incident/evidence document on current `main` contained `tiaai.online`, so no historical statement needed rewriting or preservation in this PR.

## Runtime behavior prepared by this PR

`NEXT_PUBLIC_APP_URL` is the explicit application origin for server-side Auth callback construction. Production should set it to `https://app.linkaai.online` before the application release. Signup confirmation and password recovery both construct `/auth/callback` from this configured origin.

`LINKA_LEGACY_DOMAIN_REDIRECT_ENABLED` defaults to `false`. After the new host is live and verified, it may be set to `true` to issue a 308 redirect from legacy-host requests to the canonical app while preserving path/query. The legacy `/auth/callback` route is deliberately exempt so old Supabase confirmation/recovery links can finish on their original host during the compatibility window.

Sessions/cookies are host-bound. Existing sessions on `app.tiaai.online` do not transfer to `app.linkaai.online`; a user may need to sign in again on the new host. This is expected. The redirect rule is one-way and only matches the legacy hostname, so it cannot create a canonical-host redirect loop.

## PR #211 compatibility

PR #211 (`Signup Email Verification Code`) is still open and changes signup files plus OTP rollout docs. Do not duplicate its OTP implementation here. After this domain migration is merged, rebase #211 onto latest `main` and resolve its signup origin through `configuredAppOrigin(...)` / `authCallbackUrl(...)`. Its docs/tests must use `https://app.linkaai.online` as the production application host. Keep its dual Supabase signup template behavior unchanged.

## Production environment changes

| Setting | Current | New | When to switch | Rollback |
| --- | --- | --- | --- | --- |
| Vercel project domain | `app.tiaai.online` | add `app.linkaai.online` | before app release | keep old domain attached |
| Frontend `NEXT_PUBLIC_APP_URL` | unset | `https://app.linkaai.online` | after new Vercel domain resolves, before release | unset / previous value |
| Frontend `LINKA_LEGACY_DOMAIN_REDIRECT_ENABLED` | unset/false | `true` only after new-host verification | late migration step | `false` |
| Railway `CORS_ORIGINS` | existing value | include both `https://app.linkaai.online` and `https://app.tiaai.online` | before first request from new host | previous value |
| Supabase Site URL | existing old/current value | `https://app.linkaai.online` | after new host is live and callbacks are allow-listed | previous value |
| Supabase redirect allow-list | includes old callback as applicable | add new callback while retaining old | before testing new Auth flows | remove new entry only if rollback |

Do not expose or replace secret values while changing these variables.

## Current frontend hosting / DNS

The production frontend is the Vercel project `tia-ai-staging` (`prj_eo65bi5wrPCuC1PT5CogQ1Tw7AOO`). At audit time `app.tiaai.online` is attached and verified. `linkaai.online` is not currently attached to this Vercel team/project.

Required hosting work before release:

1. Add `app.linkaai.online` to the same Vercel project.
2. Follow the verification/DNS record returned by Vercel for that domain; do not guess the record before Vercel provides it.
3. Verify TLS and production routing on the new hostname.
4. Keep `app.tiaai.online` attached through the compatibility window.

The Railway-generated API hostname remains unchanged; this migration does not create an API custom domain.

## Supabase Auth transition

Before switching canonical Auth URLs:

1. Add `https://app.linkaai.online/auth/callback` to allowed redirects.
2. Retain `https://app.tiaai.online/auth/callback` temporarily.
3. Verify login on the new host.
4. Verify signup confirmation callback on the new host.
5. Verify password recovery -> callback -> `/reset-password` on the new host.
6. Only then make the new Site URL canonical.

Do not blindly redirect legacy `/auth/callback`; old links may carry PKCE/token state associated with the old host/cookies.

## External configuration checklist

Update only settings that are actually configured in the corresponding provider:

- Meta App domains / website URL: change product-facing host to `linkaai.online` / `app.linkaai.online` as applicable.
- Privacy Policy URL: `https://app.linkaai.online/privacy`.
- Data deletion URL: `https://app.linkaai.online/data-deletion`.
- Terms URL: no dedicated Linka terms route was found in the repo; do not invent one.
- OAuth redirect URIs: update only for integrations that currently use the frontend host; keep required old callbacks during transition.
- WhatsApp webhook callback: current backend constructs its public callback from the backend request host; do not replace the Railway backend domain as part of this task.
- Future sender addresses are intended to use `no-reply@linkaai.online` and support can use `support@linkaai.online`, but SMTP/Resend/mailboxes are not configured by this migration.

## Zero-downtime rollout

1. Merge application support for the canonical origin and disabled-by-default compatibility redirect.
2. Add and verify `app.linkaai.online` on Vercel without removing the old domain.
3. Add the new Supabase callback/redirect URL while retaining the old one.
4. Add the new frontend origin to Railway `CORS_ORIGINS` while retaining the old one.
5. Set `NEXT_PUBLIC_APP_URL=https://app.linkaai.online` and deploy the frontend.
6. Verify the new domain loads and TLS is valid.
7. Verify login/session behavior; expect pre-existing old-host sessions to require login again.
8. Verify signup confirmation and password recovery callbacks on the new domain.
9. Update configured Meta/privacy/data-deletion/public URLs.
10. Make the new domain canonical in user-facing documentation and Supabase Site URL.
11. Keep the old Vercel domain and old Supabase callback operational during observation.
12. Enable `LINKA_LEGACY_DOMAIN_REDIRECT_ENABLED=true` only after the new host is proven. `/auth/callback` remains exempt.
13. Remove old-domain compatibility in a later cleanup after observation, not in this release.

## Rollback plan

If the new domain fails before canonical switch, keep serving the old domain, keep the redirect flag disabled, restore the prior CORS/Supabase values, and remove only the new domain/allow-list additions if needed. If failure occurs after canonical switch, set `LINKA_LEGACY_DOMAIN_REDIRECT_ENABLED=false`, restore the prior Supabase Site URL and frontend app URL, and route users through the still-attached old Vercel domain. Do not remove the old callback/domain until the observation window is complete.
