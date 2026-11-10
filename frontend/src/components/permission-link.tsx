"use client";

import Link from "next/link";
import { createContext, useContext, type ComponentProps, type ReactNode } from "react";
import {
  canAccessRoute,
} from "@/lib/section-access";
import type { WorkspaceAccess } from "@/lib/types";

const SectionAccessContext = createContext<WorkspaceAccess | null>(null);

export function SectionAccessProvider({
  workspace,
  children,
}: {
  workspace: WorkspaceAccess;
  children: ReactNode;
}) {
  return (
    <SectionAccessContext.Provider value={workspace}>
      {children}
    </SectionAccessContext.Provider>
  );
}

type PermissionLinkProps = ComponentProps<typeof Link>;

export function PermissionLink(props: PermissionLinkProps) {
  const workspace = useContext(SectionAccessContext);
  const href = props.href;
  const pathname =
    typeof href === "string"
      ? href
      : typeof href === "object" && href !== null && "pathname" in href
        ? String(href.pathname ?? "")
        : "";
  if (pathname && !canAccessRoute(workspace, pathname)) return null;

  return <Link {...props} />;
}
