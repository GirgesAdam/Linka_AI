# Supabase signup email OTP rollout

The signup UI verifies a six-digit Supabase confirmation token inside Linka. Supabase remains the authentication authority; Linka does not store or deliver OTPs.

## Root-cause evidence from production

Production Auth evidence showed two separate link-flow risks during the domain migration:

- A one-time confirmation link can be consumed and later replayed, producing `One-time token not found` / invalid-or-expired semantics.
- The legacy `ConfirmationURL` PKCE flow can verify the email successfully but still fail the browser session exchange with `flow_state_expired` when confirmation happens outside the browser/session that initiated signup.

The production-safe direction is therefore: use the six-digit OTP as the primary signup experience, and use a direct `TokenHash` callback as the link fallback. Do not restore raw `{{ .ConfirmationURL }}` as the fallback.

## Hosted project requirements

- Email provider: enabled.
- Confirm email: enabled (`mailer_autoconfirm = false`).
- Confirm signup email template: use `frontend/supabase-email-templates/confirm-signup.html`.
- The six-digit `{{ .Token }}` is the **primary** confirmation mechanism.
- The fallback link must send `{{ .TokenHash }}` directly to Linka `/auth/callback` with `type=email`.
- Canonical production application host: `https://app.linkaai.online`.
- The canonical signup and password-recovery callback URLs must remain allowed.
- Legacy `https://app.tiaai.online` callback URLs remain allowlisted during the zero-downtime migration window.
- Keep `/auth/callback`: password recovery and token-hash link flows depend on it.

## Zero-downtime rollout order

1. Keep the currently working TokenHash confirmation template until the OTP-capable application build is ready to deploy.
2. Immediately before deploying the OTP-capable build, apply the dual code + TokenHash fallback hosted signup template from this branch.
3. Verify the fallback link still reaches `https://app.linkaai.online/auth/callback` and does not use raw `ConfirmationURL` PKCE exchange.
4. Merge/deploy the application version containing the in-app OTP screen.
5. Run one disposable end-to-end signup through six-digit code verification.
6. Keep the dual template initially after release; removing the fallback link is a later cleanup and is not part of this release.

This ordering avoids a transition gap: the pre-deploy UI keeps the already-verified TokenHash link, while the new UI receives the six-digit code as soon as the dual template is applied.

The recovery/invite/email-change templates are intentionally not changed by this task.
