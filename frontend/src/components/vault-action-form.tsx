"use client";

import { useActionState, useEffect, useRef, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { LoaderCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { initialActionFeedbackState, type ActionFeedbackState } from "@/lib/action-feedback";

type Props = {
  action: (prev: ActionFeedbackState, formData: FormData) => Promise<ActionFeedbackState>;
  children: ReactNode;
  confirmMessage: string;
  submitLabel: string;
  pendingLabel: string;
  className?: string;
  disabled?: boolean;
};

export function VaultActionForm({
  action, children, confirmMessage, submitLabel, pendingLabel, className, disabled,
}: Props) {
  const idempotencyKey = useRef<string | null>(null);
  const [state, formAction, pending] = useActionState(action, initialActionFeedbackState);
  const router = useRouter();

  useEffect(() => {
    if (!state.notice) return;
    idempotencyKey.current = null;
    router.refresh();
  }, [router, state.notice]);

  function confirm(event: FormEvent<HTMLFormElement>) {
    if (!window.confirm(confirmMessage)) {
      event.preventDefault();
      return;
    }
    // A UUID is created only on a real submit. Retries reuse the same key,
    // while a successful commit rotates it without render-time randomness.
    if (!idempotencyKey.current) idempotencyKey.current = crypto.randomUUID();
    const field = event.currentTarget.elements.namedItem("idempotency_key");
    if (!(field instanceof HTMLInputElement)) {
      event.preventDefault();
      return;
    }
    field.value = idempotencyKey.current;
  }

  return (
    <form action={formAction} onSubmit={confirm} className={className}>
      <input type="hidden" name="idempotency_key" defaultValue="" />
      {children}
      {state.error ? <p role="alert" aria-live="polite" className="rounded-lg bg-red-50 p-2 text-xs font-bold text-red-700">{state.error}</p> : null}
      {state.notice ? <p role="status" aria-live="polite" className="rounded-lg bg-emerald-50 p-2 text-xs font-bold text-emerald-800">{state.notice}</p> : null}
      <Button type="submit" disabled={pending || disabled}>
        {pending ? <><LoaderCircle size={15} className="animate-spin" /> {pendingLabel}</> : submitLabel}
      </Button>
    </form>
  );
}
