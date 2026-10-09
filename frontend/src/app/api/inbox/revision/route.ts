import { NextRequest, NextResponse } from "next/server";

import { tiaRequest } from "@/lib/tia/api";

export const dynamic = "force-dynamic";

type InboxRevision = {
  revision: string;
};

export async function GET(request: NextRequest) {
  const conversationId = request.nextUrl.searchParams.get("conversation_id");
  const query = conversationId
    ? `?conversation_id=${encodeURIComponent(conversationId)}`
    : "";

  const revision = await tiaRequest<InboxRevision>(`/inbox/revision${query}`).catch(
    () => null,
  );

  if (!revision) {
    return NextResponse.json(
      { error: "Unable to load inbox revision." },
      { status: 502 },
    );
  }

  return NextResponse.json(revision, { headers: { "Cache-Control": "no-store" } });
}
