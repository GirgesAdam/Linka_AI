import { NextRequest, NextResponse } from "next/server";

import { tiaRequest } from "@/lib/tia/api";

export const dynamic = "force-dynamic";

type LiveRevision = { revision: string };

const ALLOWED_QUERY_KEYS = new Set([
  "limit",
  "offset",
  "owner_type",
  "status",
  "assigned_to_me",
  "unread_only",
  "q",
]);

export async function GET(request: NextRequest) {
  const input = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  for (const [key, value] of input.entries()) {
    if (ALLOWED_QUERY_KEYS.has(key)) query.append(key, value);
  }

  const revision = await tiaRequest<LiveRevision>(
    `/inbox/conversations/live-revision?${query.toString()}`,
  ).catch(() => null);
  if (!revision) {
    return NextResponse.json({ error: "Unable to check inbox freshness." }, { status: 502 });
  }

  return NextResponse.json(revision, { headers: { "Cache-Control": "no-store" } });
}
