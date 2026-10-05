# Supabase signup email OTP rollout

The signup UI verifies a six-digit Supabase confirmation token inside Linka. Supabase remains the authentication authority; Linka does not store or deliver OTPs.

## Hosted project requirements

- Email provider: enabled.
- Confirm email: enabled (`mailer_autoconfirm = false`).
- Confirm signup email template: use `frontend/supabase-email-templates/confirm-signup.html`, whose primary confirmation mechanism is `{{ .Token }}`.
- Production application host: `https://app.tiaai.online`.
- Signup fallback redirect and password recovery callback: `https://app.tiaai.online/auth/callback` must remain allowed.
- Keep `/auth/callback`: password recovery and any remaining PKCE/token-hash link flows depend on it.

## Safe rollout order

Do not replace the hosted confirmation template with a code-only template before the application version containing the code-entry UI is live. That would strand new production signups on the old UI. Apply the hosted template at the same release boundary as the merged frontend change, then verify one disposable non-production signup mailbox if available.

The recovery/invite/email-change templates are intentionally not changed by this task.
