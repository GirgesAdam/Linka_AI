"use client";

import { useCallback, useEffect, useState, type ComponentProps, type ReactNode } from "react";
import { CheckCircle2 } from "lucide-react";
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
  successMessage = "تم تنفيذ العملية بنجاح.",
}: {
  action: MutationAction;
  storageKey: string;
  className?: string;
  successHref?: string;
  successMessage?: string;
  children: ReactNode;
}) {
  const router = useRouter();
  const [notice, setNotice] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
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
      setErrorMessage(null);
      try {
        await action(formData);
      } catch (error) {
        setNotice(null);
        setErrorMessage(error instanceof Error ? error.message : "تعذر تنفيذ العملية. حاول مرة أخرى.");
        return;
      }
      setNotice(successMessage);

      const nextToken = createClientRequestId();
      window.sessionStorage.setItem(storageKey, nextToken);
      if (successHref) {
        router.replace(successHref);
      } else {
        router.refresh();
      }
    },
    [action, router, storageKey, successHref, successMessage],
  );

  return (
    <div className="space-y-2">
      <form action={submit} className={className}>
        {children}
      </form>
      {errorMessage ? <div role="alert" aria-live="polite" className="rounded-lg bg-red-50 px-3 py-2 text-xs font-bold text-red-700">{errorMessage}</div> : notice ? <div role="status" aria-live="polite" className="flex items-center gap-1.5 rounded-lg bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800"><CheckCircle2 size={14} />{notice}</div> : null}
    </div>
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
