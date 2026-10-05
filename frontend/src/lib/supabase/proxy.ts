import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { legacyDomainRedirectUrl } from "@/lib/production-domain";

const PUBLIC_PATHS = new Set([
  "/login",
  "/signup",
  "/forgot-password",
  "/reset-password",
  "/auth/callback",
  "/privacy",
  "/data-deletion",
]);

function safeDestination(value: string | null) {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/login")) return "/dashboard";
  return value;
}

export async function updateSession(request: NextRequest) {
  const legacyRedirect = legacyDomainRedirectUrl(
    request.nextUrl,
    process.env.LINKA_LEGACY_DOMAIN_REDIRECT_ENABLED === "true",
  );
  if (legacyRedirect) {
    return NextResponse.redirect(legacyRedirect, 308);
  }

  let response = NextResponse.next({ request });
  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    },
  );

  const { data } = await supabase.auth.getClaims();
  const signedIn = Boolean(data?.claims?.sub);
  const pathname = request.nextUrl.pathname;
  const isLogin = pathname === "/login";
  const isPublicPath = PUBLIC_PATHS.has(pathname);

  if (!signedIn && !isPublicPath) {
    const url = request.nextUrl.clone();
    const destination = `${pathname}${request.nextUrl.search}`;
    url.pathname = "/login";
    url.search = "";
    url.searchParams.set("next", destination);
    return NextResponse.redirect(url);
  }

  if (signedIn && isLogin) {
    const url = request.nextUrl.clone();
    url.pathname = safeDestination(request.nextUrl.searchParams.get("next"));
    url.search = "";
    return NextResponse.redirect(url);
  }

  return response;
}
