import "server-only";

import { createHash } from "node:crypto";

import type { ChannelConnection } from "@/lib/types";

function sha256Revision(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export function inboxChannelHealthRevision(connections: ChannelConnection[]): string {
  return sha256Revision(
    connections.map((connection) => ({
      id: connection.id,
      channel: connection.channel,
      provider: connection.provider,
      status: connection.status,
      updated_at: connection.updated_at,
      provider_health: connection.config_json?.provider_health ?? null,
    })),
  );
}
