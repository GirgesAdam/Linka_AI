export const appointmentLabels: Record<string, string> = {
  pending: "قيد الانتظار",
  confirmed: "مؤكد",
  checked_in: "وصل",
  in_progress: "داخل الجلسة",
  completed: "مكتمل",
  cancelled: "ملغي",
  no_show: "لم يحضر",
  rescheduled: "تم تغيير الموعد",
};

export const statusLabels: Record<string, string> = {
  active: "نشط",
  inactive: "غير نشط",
  open: "مفتوحة",
  closed: "مغلقة",
  pending: "قيد الانتظار",
  confirmed: "مؤكد",
  completed: "مكتمل",
  cancelled: "ملغي",
  failed: "تحتاج مراجعة",
  queued: "بانتظار التنفيذ",
  processing: "جارٍ التنفيذ",
  dispatched: "تم الإرسال",
  sent: "تم الإرسال",
  received: "تم الاستلام",
  delivered: "تم التسليم",
  read: "مقروء",
  paused: "متوقف مؤقتًا",
  disconnected: "غير متصل",
  connected: "متصل",
  resolved: "تمت المتابعة",
  claimed: "قيد المتابعة",
  in_progress: "قيد التنفيذ",
  enabled: "مفعّل",
  disabled: "متوقف",
};

export const priorityLabels: Record<string, string> = {
  low: "منخفضة",
  normal: "عادية",
  medium: "متوسطة",
  high: "مرتفعة",
  urgent: "عاجلة",
};

export const channelLabels: Record<string, string> = {
  whatsapp: "واتساب",
  web: "الموقع",
  instagram: "إنستجرام",
  messenger: "ماسنجر",
  phone: "هاتف",
  email: "بريد إلكتروني",
};

export const sourceLabels: Record<string, string> = {
  manual: "إدخال يدوي",
  admin: "فريق العيادة",
  ai: "Linka",
  whatsapp: "واتساب",
  web: "الموقع",
  widget: "الحجز الإلكتروني",
  booking_widget: "الحجز الإلكتروني",
  import: "بيانات مستوردة",
  integration: "نظام متصل",
  api: "نظام متصل",
};

export type BadgeTone = "green" | "yellow" | "red" | "blue" | "gray" | "purple";
export type StatusIntent = "success" | "warning" | "danger" | "info" | "neutral" | "accent";
export type StatusIconKey = "check" | "clock" | "user-check" | "activity" | "x" | "user-x" | "calendar" | "message" | "alert" | "pause" | "link" | "dot";
export type StatusDomain = "appointment" | "conversation" | "handoff" | "automation" | "patient" | "task" | "connection" | "message" | "priority" | "generic";

export type StatusPresentation = {
  label: string;
  intent: StatusIntent;
  tone: BadgeTone;
  icon: StatusIconKey;
};

const toneByIntent: Record<StatusIntent, BadgeTone> = {
  success: "green",
  warning: "yellow",
  danger: "red",
  info: "blue",
  neutral: "gray",
  accent: "purple",
};

function status(label: string, intent: StatusIntent, icon: StatusIconKey): StatusPresentation {
  return { label, intent, icon, tone: toneByIntent[intent] };
}

const appointmentStatus: Record<string, StatusPresentation> = {
  pending: status("قيد الانتظار", "warning", "clock"),
  confirmed: status("مؤكد", "success", "check"),
  checked_in: status("وصل", "info", "user-check"),
  in_progress: status("داخل الجلسة", "info", "activity"),
  completed: status("مكتمل", "success", "check"),
  cancelled: status("ملغي", "danger", "x"),
  no_show: status("لم يحضر", "danger", "user-x"),
  rescheduled: status("تم تغيير الموعد", "info", "calendar"),
};

const domainStatus: Record<Exclude<StatusDomain, "appointment" | "generic">, Record<string, StatusPresentation>> = {
  conversation: {
    open: status("مفتوحة", "info", "message"),
    pending: status("بانتظار رد", "warning", "clock"),
    closed: status("مغلقة", "neutral", "check"),
  },
  handoff: {
    open: status("تحتاج تدخل الفريق", "warning", "alert"),
    claimed: status("قيد المتابعة", "info", "user-check"),
    in_progress: status("قيد المتابعة", "info", "activity"),
    resolved: status("تمت المتابعة", "neutral", "check"),
    closed: status("مغلقة", "neutral", "check"),
  },
  automation: {
    queued: status("بانتظار التنفيذ", "warning", "clock"),
    processing: status("جارٍ التنفيذ", "info", "activity"),
    dispatched: status("تم الإرسال", "info", "check"),
    sent: status("تم الإرسال", "info", "check"),
    delivered: status("تم التسليم", "success", "check"),
    failed: status("تحتاج مراجعة", "danger", "alert"),
    paused: status("متوقف مؤقتًا", "warning", "pause"),
  },
  patient: {
    active: status("نشط", "success", "check"),
    inactive: status("غير نشط", "neutral", "pause"),
    blocked: status("محظور", "danger", "x"),
  },
  task: {
    open: status("مفتوحة", "warning", "clock"),
    pending: status("قيد الانتظار", "warning", "clock"),
    in_progress: status("قيد التنفيذ", "info", "activity"),
    completed: status("مكتمل", "success", "check"),
    cancelled: status("ملغي", "neutral", "x"),
  },
  connection: {
    connected: status("متصل", "success", "link"),
    disconnected: status("غير متصل", "neutral", "link"),
    enabled: status("مفعّل", "success", "check"),
    disabled: status("متوقف", "neutral", "pause"),
  },
  message: {
    queued: status("بانتظار الإرسال", "warning", "clock"),
    processing: status("جارٍ الإرسال", "info", "activity"),
    dispatched: status("تم الإرسال", "info", "check"),
    sent: status("تم الإرسال", "info", "check"),
    received: status("تم الاستلام", "info", "check"),
    delivered: status("تم التسليم", "success", "check"),
    read: status("مقروء", "success", "check"),
    failed: status("فشل الإرسال", "danger", "alert"),
  },
  priority: {
    low: status("منخفضة", "neutral", "dot"),
    normal: status("عادية", "neutral", "dot"),
    medium: status("متوسطة", "info", "dot"),
    high: status("مرتفعة", "warning", "alert"),
    urgent: status("عاجلة", "danger", "alert"),
  },
};

const genericStatus: Record<string, StatusPresentation> = {
  active: status("نشط", "success", "check"),
  inactive: status("غير نشط", "neutral", "pause"),
  open: status("مفتوحة", "info", "dot"),
  closed: status("مغلقة", "neutral", "check"),
  pending: status("قيد الانتظار", "warning", "clock"),
  confirmed: status("مؤكد", "success", "check"),
  completed: status("مكتمل", "success", "check"),
  cancelled: status("ملغي", "danger", "x"),
  failed: status("تحتاج مراجعة", "danger", "alert"),
  queued: status("بانتظار التنفيذ", "warning", "clock"),
  processing: status("جارٍ التنفيذ", "info", "activity"),
  dispatched: status("تم الإرسال", "info", "check"),
  sent: status("تم الإرسال", "info", "check"),
  received: status("تم الاستلام", "info", "check"),
  delivered: status("تم التسليم", "success", "check"),
  read: status("مقروء", "success", "check"),
  paused: status("متوقف مؤقتًا", "warning", "pause"),
  disconnected: status("غير متصل", "neutral", "link"),
  connected: status("متصل", "success", "link"),
  resolved: status("تمت المتابعة", "neutral", "check"),
  claimed: status("قيد المتابعة", "info", "user-check"),
  in_progress: status("قيد التنفيذ", "info", "activity"),
  enabled: status("مفعّل", "success", "check"),
  disabled: status("متوقف", "neutral", "pause"),
  checked_in: status("وصل", "info", "user-check"),
  no_show: status("لم يحضر", "danger", "user-x"),
  rescheduled: status("تم تغيير الموعد", "info", "calendar"),
  urgent: status("عاجلة", "danger", "alert"),
  high: status("مرتفعة", "warning", "alert"),
};

export function statusPresentation(domain: StatusDomain, value: string | null | undefined): StatusPresentation {
  if (!value) return status("غير محدد", "neutral", "dot");
  const normalized = value.toLowerCase();
  if (domain === "appointment") return appointmentStatus[normalized] || genericStatus[normalized] || status("غير محدد", "neutral", "dot");
  if (domain !== "generic") return domainStatus[domain][normalized] || genericStatus[normalized] || status("غير محدد", "neutral", "dot");
  return genericStatus[normalized] || status(labelForStatus(normalized), "neutral", "dot");
}

export function labelForStatus(value: string | null | undefined) {
  if (!value) return "—";
  return appointmentLabels[value] || statusLabels[value] || "غير محدد";
}

export function labelForPriority(priority: string | null | undefined) {
  if (!priority) return "—";
  return priorityLabels[priority] || "عادية";
}

export function labelForChannel(channel: string | null | undefined) {
  if (!channel) return "—";
  return channelLabels[channel.toLowerCase()] || "قناة تواصل";
}

export function labelForSource(source: string | null | undefined) {
  if (!source) return "—";
  return sourceLabels[source.toLowerCase()] || "مصدر خارجي";
}

export const toneForStatus = (value: string): BadgeTone => statusPresentation("generic", value).tone;
