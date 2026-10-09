import { NextResponse } from "next/server";

import { tiaRequest } from "@/lib/tia/api";

export const dynamic = "force-dynamic";

type InboxSummary = {
  unread_conversations: number;
};

export async function GET() {
  const summary = await tiaRequest<InboxSummary>("/inbox/summary").catch(() => null);

  if (!summary) {
    return NextResponse.json(
      { error: "Unable to load inbox summary." },
      { status: 502 },
    );
  }

  return NextResponse.json(summary, { headers: { "Cache-Control": "no-store" } });
}
