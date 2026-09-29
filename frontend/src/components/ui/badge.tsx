import { cn } from "@/lib/utils";

const styles = {
  green: "bg-[var(--status-success-soft)] text-[var(--status-success)] ring-emerald-600/15",
  yellow: "bg-[var(--status-warning-soft)] text-[var(--status-warning)] ring-amber-600/15",
  red: "bg-[var(--status-danger-soft)] text-[var(--status-danger)] ring-rose-600/15",
  blue: "bg-[var(--status-info-soft)] text-[var(--status-info)] ring-sky-600/15",
  gray: "bg-[var(--status-neutral-soft)] text-[var(--status-neutral)] ring-slate-500/15",
  purple: "bg-[var(--accent-soft)] text-[var(--accent-strong)] ring-[var(--accent-border)]",
} as const;

export function Badge({
  children,
  tone = "gray",
  className,
}: {
  children: React.ReactNode;
  tone?: keyof typeof styles;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex min-h-6 items-center rounded-full px-2.5 py-0.5 text-[11px] font-bold leading-5 ring-1 ring-inset",
        styles[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
