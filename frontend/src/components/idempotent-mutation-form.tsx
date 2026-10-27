"use client";

import { useCallback, useEffect, type ComponentProps, type ReactNode } from "react";
import { useFormStatus } from "react-dom";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { createClientRequestId } from "@/lib/utils";
import { isOperationToken } from "@/lib/tia/operation-idempotency";

type MutationAction = (formData: FormData) => Promise<void>;

function storedToken(storageKey: string): string {
  const existing = window.sessionStorage.getItem(storageKey);
  if (existing && isOperationToken(existing)) return existing;
  const created = createClientRequestId();
  window.sessionStorage.setItem(storageKey, created);
  return created;
}

export function IdempotentMutationForm({
  action,
  storageKey,
  className,
  successHref,
  children,
}: {
  action: MutationAction;
  storageKey: string;
  className?: string;
  successHref?: string;
  children: ReactNode;
}) {
  const router = useRouter();
  useEffect(() => {
    storedToken(storageKey);
  }, [storageKey]);

  const submit = useCallback(
    async (formData: FormData) => {
      const currentToken = storedToken(storageKey);
      window.sessionStorage.setItem(storageKey, currentToken);
      formData.set("operation_token", currentToken);

      // Rotate only after a confirmed successful server action. If the request
      // result is unknown (network error/redirected failure), the persisted token
      // survives so a retry represents the same logical operation.
      await action(formData);

      const nextToken = createClientRequestId();
      window.sessionStorage.setItem(storageKey, nextToken);
      if (successHref) {
        router.replace(successHref);
      } else {
        router.refresh();
      }
    },
    [action, router, storageKey, successHref],
  );

  return (
    <form action={submit} className={className}>
      {children}
    </form>
  );
}

export function PendingSubmitButton({ pendingLabel, children, disabled, ...props }: ComponentProps<typeof Button> & { pendingLabel?: ReactNode }) {
  const { pending } = useFormStatus();
  return (
    <Button {...props} disabled={disabled || pending}>
      {pending && pendingLabel ? pendingLabel : children}
    </Button>
  );
}
