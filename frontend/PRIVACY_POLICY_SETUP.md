# Linka public privacy and deletion pages

Linka exposes two public pages required for the Meta/WhatsApp app setup:

- `/privacy`
- `/data-deletion`

The Supabase session proxy explicitly allows both routes without authentication so
Meta reviewers and customers can access them without logging in.

## Public values

Set these in the frontend deployment environment:

```text
NEXT_PUBLIC_TIA_LEGAL_NAME=Linka
NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL=privacy@your-real-domain.com
```

Use an email address that is real and actively monitored. Do not publish a fake
address just to satisfy a form.

If no privacy email is configured, the pages direct customers to the clinic or
business they interacted with and direct workspace administrators to their normal
Linka support channel.

## Deployment check

After deployment, open both pages in a private/incognito browser window while
logged out:

```text
https://YOUR_FRONTEND_DOMAIN/privacy
https://YOUR_FRONTEND_DOMAIN/data-deletion
```

Both pages must render directly and must not redirect to `/login`.

Then configure Meta with:

```text
Privacy Policy URL:
https://YOUR_FRONTEND_DOMAIN/privacy

Data deletion instructions URL:
https://YOUR_FRONTEND_DOMAIN/data-deletion
```

## Scope

The privacy policy covers:

- clinic/workspace configuration;
- customer CRM information;
- appointments;
- WhatsApp messages and delivery metadata;
- AI-assisted processing;
- human handoff;
- automation/integration providers;
- retention, security, international processing, rights, and contact.

The policy does not claim that AI provides medical diagnosis. It states that
medical diagnosis, treatment suitability, and urgent clinical concerns should be
handled by qualified human professionals.
