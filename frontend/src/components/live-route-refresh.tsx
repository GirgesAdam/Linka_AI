"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

type RevisionResponse = { revision?: unknown };

export function LiveRouteRefresh({
  endpoint,
  initialRevision,
  intervalMs = 10_000,
}: {
  endpoint: string;
  initialRevision: string;
  intervalMs?: number;
}) {
  const router = useRouter();

  useEffect(() => {
    let active = true;
    let inFlight = false;
    let currentRevision = initialRevision;

    const checkForChange = async () => {
      if (!active || inFlight || document.visibilityState !== "visible") return;
      inFlight = true;
      try {
        const response = await fetch(endpoint, { cache: "no-store" });
        if (!response.ok) return;
        const payload = (await response.json()) as RevisionResponse;
        if (typeof payload.revision !== "string" || payload.revision === currentRevision) return;
        currentRevision = payload.revision;
        router.refresh();
      } catch {
        // The normal interval remains the only retry mechanism; avoid tight retry loops.
      } finally {
        inFlight = false;
      }
    };

    const interval = window.setInterval(() => {
      void checkForChange();
    }, intervalMs);
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void checkForChange();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      active = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [endpoint, initialRevision, intervalMs, router]);

  return null;
}
