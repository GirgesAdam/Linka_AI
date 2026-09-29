# Linka Dashboard — v0.11.0

Next.js 16 App Router dashboard for the Linka clinic platform.

## Local setup

1. Copy `.env.example` to `.env.local`.
2. Set the same Supabase Staging project URL + publishable key used by FastAPI.
3. Set `TIA_API_URL=http://127.0.0.1:8000` for local development.
4. Run `npm install`.
5. Run `npm run dev`.
6. Open `http://localhost:3000`.

## Security model

- Supabase is used only for Auth/session management in the frontend.
- Business data is fetched from FastAPI, not Supabase Data API.
- FastAPI receives `Authorization: Bearer <Supabase access token>` and `X-Workspace-ID`.
- Workspace selection is stored in an HttpOnly cookie.
- Admin/member authorization remains enforced by FastAPI.

## Screens

- `/dashboard` — operations summary
- `/inbox` — human handoffs/team inbox
- `/appointments` — real appointments
- `/patients` — CRM patients
- `/automations` — rules/jobs
- `/channels` — connected communication channels
- `/team` — admin-only workspace team management

## Backend change

This patch adds `GET /api/v1/dashboard/summary` to avoid assembling dashboard metrics through many frontend requests.
No database migration is required.
