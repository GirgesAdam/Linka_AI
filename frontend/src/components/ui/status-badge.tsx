import {
  Activity,
  CalendarSync,
  Check,
  CircleAlert,
  CircleDot,
  Clock3,
  Link2,
  MessageCircle,
  Pause,
  UserCheck,
  UserX,
  X,
} from "lucide-react";

import { statusPresentation, type StatusDomain, type StatusIconKey } from "@/lib/status";
import { Badge } from "@/components/ui/badge";

const icons: Record<StatusIconKey, typeof Check> = {
  check: Check,
  clock: Clock3,
  "user-check": UserCheck,
  activity: Activity,
  x: X,
  "user-x": UserX,
  calendar: CalendarSync,
  message: MessageCircle,
  alert: CircleAlert,
  pause: Pause,
  link: Link2,
  dot: CircleDot,
};

export function StatusBadge({
  domain,
  status,
  label,
  showIcon = true,
  className,
}: {
  domain: StatusDomain;
  status: string | null | undefined;
  label?: string;
  showIcon?: boolean;
  className?: string;
}) {
  const presentation = statusPresentation(domain, status);
  const Icon = icons[presentation.icon];

  return (
    <Badge tone={presentation.tone} className={className}>
      {showIcon && <Icon size={11} strokeWidth={2.2} className="ml-1 shrink-0" aria-hidden="true" />}
      {label || presentation.label}
    </Badge>
  );
}
