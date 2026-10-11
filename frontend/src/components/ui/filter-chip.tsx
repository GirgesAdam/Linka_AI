import Link from "next/link";

import { cn } from "@/lib/utils";

export function FilterChip({
  href,
  active,
  children,
  className,
}: {
  href: string;
  active?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "inline-flex min-h-9 shrink-0 cursor-pointer items-center justify-center rounded-lg border px-3 text-xs font-bold transition-[background-color,border-color,color,box-shadow,transform] duration-150 motion-reduce:transition-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 active:translate-y-px motion-reduce:active:translate-y-0",
        active
          ? "border-teal-200 bg-teal-50 text-teal-800 shadow-[inset_0_0_0_1px_rgba(13,148,136,.04)]"
          : "border-transparent bg-transparent text-slate-600 hover:border-slate-200 hover:bg-slate-50 hover:text-slate-950",
        className,
      )}
    >
      {children}
    </Link>
  );
}
