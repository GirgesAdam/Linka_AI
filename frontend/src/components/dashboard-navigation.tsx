"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  Banknote,
  BarChart3,
  CalendarDays,
  ContactRound,
  History,
  Inbox,
  LayoutDashboard,
  ListTodo,
  Menu,
  PackageSearch,
  Settings2,
  Stethoscope,
  Tags,
  UsersRound,
  Workflow,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";

const inboxPollIntervalMs = 15_000;

const dailyItems = [
  { href: "/dashboard", label: "الرئيسية", icon: LayoutDashboard },
  { href: "/inbox", label: "الرسائل", icon: Inbox },
  { href: "/appointments", label: "المواعيد", icon: CalendarDays },
] as const;

const patientItems = [
  { href: "/patients", label: "العملاء", icon: ContactRound },
  { href: "/tasks", label: "المتابعات", icon: ListTodo },
] as const;

const clinicItems = [
  { href: "/doctors", label: "الدكاترة", icon: Stethoscope },
] as const;

const clinicAdminItems = [
  { href: "/services", label: "الخدمات والأسعار", icon: Tags },
  { href: "/inventory", label: "المخزن", icon: PackageSearch },
] as const;

const managementItems = [
  { href: "/analytics", label: "التقارير", icon: BarChart3 },
  { href: "/finance", label: "المالية", icon: Banknote },
] as const;

const managementAdminItems = [
  { href: "/automations", label: "الرسائل التلقائية", icon: Workflow },
  { href: "/team", label: "الفريق", icon: UsersRound },
  { href: "/setup", label: "إعدادات العيادة", icon: Settings2 },
  { href: "/activity", label: "سجل النشاط", icon: History },
] as const;

const coreItems = [...dailyItems, ...patientItems, ...clinicItems, ...managementItems];
const mobilePrimaryHrefs = new Set(["/dashboard", "/inbox", "/appointments", "/patients"]);

type InboxSummary = {
  unread_conversations: number;
};

function activeFor(pathname: string, href: string) {
  if (href === "/dashboard") return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

function useInboxUnreadCount(mediaQuery: string) {
  const [unreadCount, setUnreadCount] = useState(0);

  useEffect(() => {
    const media = window.matchMedia(mediaQuery);
    let cancelled = false;

    const refresh = async () => {
      if (cancelled || !media.matches || document.visibilityState !== "visible") return;

      try {
        const response = await fetch("/api/inbox/summary", { cache: "no-store" });
        if (!response.ok) return;

        const summary = (await response.json()) as InboxSummary;
        if (!cancelled && Number.isFinite(summary.unread_conversations)) {
          setUnreadCount(Math.max(0, Math.trunc(summary.unread_conversations)));
        }
      } catch {
        // Keep the last known count when the lightweight status refresh fails.
      }
    };

    const refreshWhenRelevant = () => {
      if (media.matches && document.visibilityState === "visible") void refresh();
    };

    void refresh();
    const intervalId = window.setInterval(() => void refresh(), inboxPollIntervalMs);
    media.addEventListener("change", refreshWhenRelevant);
    document.addEventListener("visibilitychange", refreshWhenRelevant);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      media.removeEventListener("change", refreshWhenRelevant);
      document.removeEventListener("visibilitychange", refreshWhenRelevant);
    };
  }, [mediaQuery]);

  return unreadCount;
}

function InboxUnreadBadge({ count, compact = false }: { count: number; compact?: boolean }) {
  if (count <= 0) return null;

  const text = count > 99 ? "99+" : String(count);
  return (
    <span
      aria-label={count > 99 ? "أكثر من 99 محادثة غير مقروءة" : `${count} محادثة غير مقروءة`}
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-full bg-rose-600 font-bold leading-none text-white",
        compact ? "absolute -left-2 -top-2 min-w-4 px-1 py-0.5 text-[9px]" : "min-w-5 px-1.5 py-1 text-[10px]",
      )}
    >
      {text}
    </span>
  );
}

function DesktopNavItem({
  href,
  label,
  Icon,
  active,
  unreadCount = 0,
}: {
  href: string;
  label: string;
  Icon: typeof LayoutDashboard;
  active: boolean;
  unreadCount?: number;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold transition",
        active ? "bg-white/10 text-white" : "text-slate-300 hover:bg-white/[0.06] hover:text-white",
      )}
    >
      {active && <span className="absolute right-0 h-5 w-1 rounded-l-full bg-[var(--accent)]" />}
      <Icon
        size={18}
        strokeWidth={active ? 2.1 : 1.8}
        className={active ? "text-[var(--sidebar-accent)]" : "text-[var(--sidebar-muted)] group-hover:text-slate-200"}
      />
      <span className="min-w-0 flex-1">{label}</span>
      <InboxUnreadBadge count={unreadCount} />
    </Link>
  );
}

export function DesktopNavigation({ isAdmin }: { isAdmin: boolean }) {
  const pathname = usePathname();
  const unreadCount = useInboxUnreadCount("(min-width: 1024px)");
  const groups = [
    { label: "اليوم", items: [...dailyItems] },
    { label: "العملاء والمتابعة", items: [...patientItems] },
    { label: "تشغيل العيادة", items: [...clinicItems, ...(isAdmin ? clinicAdminItems : [])] },
    { label: "الإدارة", items: [...managementItems, ...(isAdmin ? managementAdminItems : [])] },
  ];

  return (
    <nav className="flex-1 overflow-y-auto px-3 py-4 scrollbar-thin" aria-label="التنقل الرئيسي">
      {groups.map((group, index) => (
        <div key={group.label} className={cn(index > 0 && "mt-4 border-t border-white/[0.07] pt-4")}>
          <div className="px-3 pb-2 text-[10px] font-bold tracking-[0.04em] text-[var(--sidebar-muted)]">
            {group.label}
          </div>
          <div className="space-y-1">
            {group.items.map(({ href, label, icon }) => (
              <DesktopNavItem
                key={href}
                href={href}
                label={label}
                Icon={icon}
                active={activeFor(pathname, href)}
                unreadCount={href === "/inbox" ? unreadCount : 0}
              />
            ))}
          </div>
        </div>
      ))}
    </nav>
  );
}

function MobileNavLink({ href, label, Icon, onNavigate }: { href: string; label: string; Icon: typeof LayoutDashboard; onNavigate?: () => void }) {
  const pathname = usePathname();
  const active = activeFor(pathname, href);
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        "flex min-h-12 items-center gap-3 rounded-xl px-3 text-sm font-bold transition",
        active ? "bg-[var(--accent-soft)] text-[var(--accent-strong)]" : "text-slate-700 hover:bg-slate-50",
      )}
    >
      <Icon size={18} className={active ? "text-[var(--accent)]" : "text-slate-500"} />
      {label}
    </Link>
  );
}

function MobileSection({
  label,
  items,
  onNavigate,
}: {
  label: string;
  items: Array<{ href: string; label: string; icon: typeof LayoutDashboard }>;
  onNavigate: () => void;
}) {
  if (!items.length) return null;
  return (
    <section className="border-t border-slate-100 pt-4 first:border-t-0 first:pt-0">
      <div className="section-kicker px-2 pb-2">{label}</div>
      <div className="space-y-1">
        {items.map(({ href, label: itemLabel, icon }) => (
          <MobileNavLink key={href} href={href} label={itemLabel} Icon={icon} onNavigate={onNavigate} />
        ))}
      </div>
    </section>
  );
}

export function MobileNavigation({ isAdmin }: { isAdmin: boolean }) {
  const pathname = usePathname();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const unreadCount = useInboxUnreadCount("(max-width: 1023px)");
  const mobilePrimary = coreItems.filter((item) => mobilePrimaryHrefs.has(item.href));
  const morePatient = patientItems.filter((item) => !mobilePrimaryHrefs.has(item.href));
  const moreClinic = [...clinicItems, ...(isAdmin ? clinicAdminItems : [])];
  const moreManagement = [...managementItems, ...(isAdmin ? managementAdminItems : [])];
  const moreActive = [...morePatient, ...moreClinic, ...moreManagement].some((item) => activeFor(pathname, item.href));

  const close = () => dialogRef.current?.close();

  return (
    <>
      <nav
        className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-5 border-t border-slate-200/90 bg-white/95 px-2 pb-[max(.5rem,env(safe-area-inset-bottom))] pt-2 shadow-[0_-8px_24px_rgba(15,23,42,.05)] backdrop-blur-xl lg:hidden"
        aria-label="التنقل الرئيسي"
      >
        {mobilePrimary.map(({ href, label, icon: Icon }) => {
          const active = activeFor(pathname, href);
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex min-h-12 flex-col items-center justify-center gap-1 rounded-xl px-1 text-[11px] font-bold transition",
                active ? "bg-[var(--accent-soft)] text-[var(--accent-strong)]" : "text-slate-500 hover:bg-slate-50 hover:text-slate-800",
              )}
            >
              <span className="relative inline-flex">
                <Icon size={18} strokeWidth={active ? 2.2 : 1.8} />
                {href === "/inbox" && <InboxUnreadBadge count={unreadCount} compact />}
              </span>
              <span className="max-w-full truncate">{label}</span>
            </Link>
          );
        })}
        <button
          type="button"
          onClick={() => dialogRef.current?.showModal()}
          aria-haspopup="dialog"
          className={cn(
            "flex min-h-12 flex-col items-center justify-center gap-1 rounded-xl px-1 text-[11px] font-bold transition",
            moreActive ? "bg-[var(--accent-soft)] text-[var(--accent-strong)]" : "text-slate-500 hover:bg-slate-50 hover:text-slate-800",
          )}
        >
          <Menu size={18} strokeWidth={moreActive ? 2.2 : 1.8} />
          المزيد
        </button>
      </nav>

      <dialog
        ref={dialogRef}
        aria-labelledby="mobile-more-title"
        className="m-0 h-full max-h-none w-full max-w-none bg-transparent p-0 backdrop:bg-slate-950/35 lg:hidden"
        onClick={(event) => {
          if (event.target === event.currentTarget) close();
        }}
      >
        <div className="absolute inset-x-0 bottom-0 max-h-[82vh] overflow-y-auto rounded-t-3xl bg-white p-4 pb-[max(1.25rem,env(safe-area-inset-bottom))] shadow-2xl">
          <div className="mb-4 flex items-center justify-between gap-3 border-b border-slate-100 pb-3">
            <div>
              <div id="mobile-more-title" className="font-bold text-slate-950">المزيد</div>
              <div className="mt-0.5 text-xs text-slate-500">أدوات العملاء وتشغيل العيادة والإدارة</div>
            </div>
            <button
              type="button"
              onClick={close}
              className="grid size-10 place-items-center rounded-xl border border-slate-200 text-slate-600 hover:bg-slate-50"
              aria-label="إغلاق القائمة"
            >
              <X size={18} />
            </button>
          </div>

          <div className="space-y-4">
            <MobileSection label="العملاء والمتابعة" items={[...morePatient]} onNavigate={close} />
            <MobileSection label="تشغيل العيادة" items={moreClinic} onNavigate={close} />
            <MobileSection label="الإدارة" items={moreManagement} onNavigate={close} />
          </div>
        </div>
      </dialog>
    </>
  );
}
