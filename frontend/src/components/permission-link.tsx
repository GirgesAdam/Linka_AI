"use client";

import Link from "next/link";
import { createContext, useContext, type ComponentProps, type ReactNode } from "react";
import {
  canAccessPage,
  resolvePagePermissionForHref,
} from "@/lib/page-permissions";
import type { WorkspaceAccess } from "@/lib/types";

const PagePermissionsContext = createContext<WorkspaceAccess | null>(null);

export function PagePermissionsProvider({
  workspace,
  children,
}: {
  workspace: WorkspaceAccess;
  children: ReactNode;
}) {
  return (
    <PagePermissionsContext.Provider value={workspace}>
      {children}
    </PagePermissionsContext.Provider>
  );
}

type PermissionLinkProps = ComponentProps<typeof Link>;

export function PermissionLink(props: PermissionLinkProps) {
  const workspace = useContext(PagePermissionsContext);
  const href = props.href;
  const pathname =
    typeof href === "string"
      ? href
      : typeof href === "object" && href !== null && "pathname" in href
        ? String(href.pathname ?? "")
        : "";
  const requiredPage = resolvePagePermissionForHref(pathname);

  if (requiredPage && (!workspace || !canAccessPage(workspace, requiredPage))) {
    return null;
  }

  return <Link {...props} />;
}
