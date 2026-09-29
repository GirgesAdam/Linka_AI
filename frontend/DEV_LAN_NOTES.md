# Linka frontend LAN development

Next.js protects dev-only resources such as HMR by origin.

`next.config.ts` reads `NEXT_ALLOWED_DEV_ORIGINS` as a comma-separated list.
The current local default is `192.168.1.5`.

Restart `npm run dev` after changing Next configuration.

The hydration warning containing `sapling-installed="true"` is caused by the
Sapling browser extension modifying `<body>` before React hydration. Disable
Sapling for the local Linka site while testing rather than hiding the warning in
application code.
