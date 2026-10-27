import { NextResponse } from "next/server";

import { inboxChannelHealthRevision } from "@/lib/inbox-live";
import { tiaRequest } from "@/lib/tia/api";
import type { ChannelConnection } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET() {
  const connections = await tiaRequest<ChannelConnection[]>("/channels/connections").catch(
    () => null,
  );
  if (!connections) {
    return NextResponse.json({ error: "Unable to check channel health." }, { status: 502 });
  }

  return NextResponse.json(
    { revision: inboxChannelHealthRevision(connections) },
    { headers: { "Cache-Control": "no-store" } },
  );
}
