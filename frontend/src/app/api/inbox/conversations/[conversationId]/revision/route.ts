import { NextResponse } from "next/server";

import { tiaRequest } from "@/lib/tia/api";

export const dynamic = "force-dynamic";

type LiveRevision = { revision: string };

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ conversationId: string }> },
) {
  const { conversationId } = await params;
  const revision = await tiaRequest<LiveRevision>(
    `/inbox/conversations/${encodeURIComponent(conversationId)}/live-revision`,
  ).catch(() => null);
  if (!revision) {
    return NextResponse.json({ error: "Unable to check conversation freshness." }, { status: 502 });
  }

  return NextResponse.json(revision, { headers: { "Cache-Control": "no-store" } });
}
