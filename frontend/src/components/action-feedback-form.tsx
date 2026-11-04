"use client";

import { useActionState, useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, LoaderCircle, Trash2 } from "lucide-react";

import { Button, type ButtonProps } from "@/components/ui/button";
import {
  initialActionFeedbackState,
  type ActionFeedbackState,
} from "@/lib/action-feedback";

export type ActionFeedback = (
  previousState: ActionFeedbackState,
  formData: FormData,
) => Promise<ActionFeedbackState>;

type Props = {
  action: ActionFeedback;
  children: ReactNode;
  className?: string;
  submitLabel: string;
  pendingLabel?: string;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
  buttonClassName?: string;
  buttonAriaLabel?: string;
  icon?: "trash";
  disabled?: boolean;
  refreshAfterSuccess?: boolean;
};

export function ActionFeedbackForm({
  action,
  children,
  className,
  submitLabel,
  pendingLabel = "جاري الحفظ...",
  variant = "default",
  size = "default",
  buttonClassName,
  buttonAriaLabel,
  icon,
  disabled,
  refreshAfterSuccess,
}: Props) {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(
    action,
    initialActionFeedbackState,
  );

  useEffect(() => {
    if (!refreshAfterSuccess || !state.notice) return;
    const timeout = window.setTimeout(() => router.refresh(), 1600);
    return () => window.clearTimeout(timeout);
  }, [refreshAfterSuccess, router, state.notice]);

  return (
    <form action={formAction} className={className}>
      {children}
      {state.error ? (
        <div role="alert" aria-live="polite" className="basis-full col-span-full rounded-lg bg-red-50 px-3 py-2 text-xs font-bold text-red-700">
          {state.error}
        </div>
      ) : state.notice ? (
        <div role="status" aria-live="polite" className="basis-full col-span-full rounded-lg bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800">
          <CheckCircle2 size={15} className="ml-1 inline-block" />
          {state.notice}
        </div>
      ) : null}
      <Button
        type="submit"
        disabled={pending || disabled}
        variant={variant}
        size={size}
        className={buttonClassName}
        aria-label={buttonAriaLabel}
        title={buttonAriaLabel}
      >
        {pending ? (
          <>
            <LoaderCircle size={15} className="animate-spin" />
            {pendingLabel}
          </>
        ) : (
          <>
            {icon === "trash" ? <Trash2 size={14} /> : null}
            {submitLabel}
          </>
        )}
      </Button>
    </form>
  );
}