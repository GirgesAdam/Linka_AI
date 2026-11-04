"use client";

import { type FormEvent, type ReactNode } from "react";
import { useFormStatus } from "react-dom";
import { LoaderCircle, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";

type Props = {
  action: (formData: FormData) => void | Promise<void>;
  confirmMessage: string;
  ariaLabel: string;
  children?: ReactNode;
  className?: string;
};

function ConfirmDeleteSubmit({ ariaLabel }: { ariaLabel: string }) {
  const { pending } = useFormStatus();

  return (
    <Button
      type="submit"
      size="icon"
      variant="danger"
      className="size-8 rounded-lg"
      aria-label={ariaLabel}
      title={ariaLabel}
      disabled={pending}
    >
      {pending ? (
        <LoaderCircle size={14} className="animate-spin" />
      ) : (
        <Trash2 size={14} />
      )}
    </Button>
  );
}

export function ConfirmActionForm({
  action,
  confirmMessage,
  ariaLabel,
  children,
  className,
}: Props) {
  function confirm(event: FormEvent<HTMLFormElement>) {
    if (!window.confirm(confirmMessage)) event.preventDefault();
  }

  return (
    <form action={action} onSubmit={confirm} className={className}>
      {children}
      <ConfirmDeleteSubmit ariaLabel={ariaLabel} />
    </form>
  );
}