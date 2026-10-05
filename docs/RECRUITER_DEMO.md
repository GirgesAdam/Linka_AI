# Linka Portfolio / Recruiter Demo

The public portfolio entry point is the live Linka application:

- Application: `https://app.linkaai.online`
- Demo email: `demo@tiaai.online` (existing Supabase demo identity retained temporarily for compatibility; not a canonical product-domain or sender-mailbox reference)
- Demo password: `TiaDemo2026!`
- Public role: `member`

The credentials above are intentionally public and belong only to the synthetic portfolio workspace. They are not infrastructure secrets and do not grant access to a real clinic.

## What the demo contains

The showcase dataset is synthetic but shaped like a working aesthetic clinic. It includes:

- patients and appointment history;
- upcoming and completed bookings;
- services and doctor schedules;
- laser hair-removal services with Prime Lase / Candela Gentle device pricing;
- cash, Visa and InstaPay transactions, including a few partial balances;
- clinic retail products and appointment product sales;
- injectable/treatment inventory in mL with concentration in mg/mL;
- inventory usage history;
- packages, CRM and analytics data already present in the demo workspace.

The account is a normal demo member so recruiters can explore operational workflows without receiving public access to destructive clinic/team configuration.

## Isolated demo deployment option

For a completely disposable recruiter environment, Linka also supports a dedicated demo deployment. Do not point that environment at real clinic data.

Backend:

```env
ENVIRONMENT=demo
DEMO_MODE=true
DEMO_ALLOW_EXTERNAL_DISPATCH=false
DEMO_AGENT_HOURLY_TURN_LIMIT=60
```

Frontend:

```env
TIA_DEMO_ENABLED=true
NEXT_PUBLIC_TIA_DEMO_ENABLED=true
TIA_DEMO_EMAIL=demo@tiaai.online
TIA_DEMO_PASSWORD=TiaDemo2026!
```

`TIA_DEMO_EMAIL` and `TIA_DEMO_PASSWORD` are server-only when the one-click demo action is enabled. The same values may also be published in the portfolio README because this account is intentionally public.

## Rebuilding an isolated recruiter workspace

Run migrations first, then seed the recruiter workspace:

```bash
cd backend
alembic upgrade head
python scripts/seed_recruiter_demo.py
```

The seed is intended for `DEMO_MODE=true` and refuses a production environment. It creates synthetic clinic configuration and recruiter-friendly patient personas. Use `bootstrap_admin.py` only when building a separate private/admin demo account; the public portfolio account should remain a member.

## Safety and reset policy

- No real patient or clinic exports belong in the portfolio workspace.
- External provider dispatch should stay disabled in a disposable demo environment.
- Public demo records may be reset periodically.
- Provider/API/database secrets must never be committed to the repository.
