"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

type RevisionResponse = {
  revision: string;
};

export function LiveRouteRefresh({
  initialRevision,
  watchUrl,
  intervalMs = 3000,
}: {
  initialRevision: string;
  watchUrl: string;
  intervalMs?: number;
}) {
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let currentRevision = initialRevision;
    let controller: AbortController | null = null;

    const checkForChange = async () => {
      if (cancelled || inFlight || document.visibilityState !== "visible") return;

      inFlight = true;
      controller = new AbortController();
      try {
        const response = await fetch(watchUrl, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) return;

        const payload = (await response.json()) as RevisionResponse;
        if (
          !cancelled &&
          document.visibilityState === "visible" &&
          typeof payload.revision === "string" &&
          payload.revision !== currentRevision
        ) {
          currentRevision = payload.revision;
          router.refresh();
        }
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          // Keep the current screen when the lightweight revision check fails.
        }
      } finally {
        inFlight = false;
        controller = null;
      }
    };

    const interval = window.setInterval(() => void checkForChange(), intervalMs);
    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        controller?.abort();
        return;
      }
      void checkForChange();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [initialRevision, intervalMs, router, watchUrl]);

  return null;
}
