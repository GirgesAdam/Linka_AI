# Supabase signup email confirmation

## Product decision

Primary signup verification is a one-click email confirmation link.

- No in-app OTP code entry.
- No manual code copy/paste.
- No raw `{{ .ConfirmationURL }}` signup flow.
- Confirmation uses `{{ .TokenHash }}` and Linka `/auth/callback`.

## Desired flow

`email + password -> signUp -> check-email state -> click confirmation link -> /auth/callback -> verifyOtp(token_hash) -> session -> /onboarding`

The application may keep only a short-lived HttpOnly pending-email cookie for displaying the normalized email, resending the confirmation email, and changing the email. It must never store the password, token, OTP, or auth secret.

## Versioned template

`frontend/supabase-email-templates/confirm-signup.html` uses the canonical Supabase `{{ .SiteURL }}` plus `{{ .TokenHash }}`:

`{{ .SiteURL }}/auth/callback?next=/onboarding&token_hash={{ .TokenHash }}&type=email`

Production Site URL is `https://app.linkaai.online`.

## Callback contract

`frontend/src/app/auth/callback/route.ts` supports both:

- `token_hash + type -> supabase.auth.verifyOtp(...)`
- `code -> supabase.auth.exchangeCodeForSession(...)`

The `code` path stays because other Auth flows may still use it. `safeNext()` only accepts local paths beginning with `/` and rejects protocol-relative `//...` redirects.

## Resend

Resend uses `auth.resend({ type: "signup", email, options: { emailRedirectTo } })`. It must never call `signUp()` a second time.

## Production rollout

If the hosted Confirm signup template already uses a TokenHash link into Linka and a fresh cross-browser confirmation succeeds, do not mutate production unnecessarily. Keep the repository template as the canonical desired form and document the production evidence.
