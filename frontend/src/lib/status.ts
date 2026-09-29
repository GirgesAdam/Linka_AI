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

export function labelForStatus(status: string | null | undefined) {
  if (!status) return "—";
  return appointmentLabels[status] || statusLabels[status] || "غير محدد";
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

export const toneForStatus = (s: string): "green" | "yellow" | "red" | "blue" | "gray" | "purple" =>
  ({
    confirmed: "green",
    completed: "green",
    active: "green",
    connected: "green",
    enabled: "green",
    delivered: "green",
    read: "green",
    pending: "yellow",
    queued: "yellow",
    processing: "blue",
    checked_in: "blue",
    in_progress: "blue",
    claimed: "blue",
    dispatched: "blue",
    sent: "blue",
    received: "blue",
    failed: "red",
    cancelled: "red",
    no_show: "red",
    urgent: "red",
    high: "yellow",
    resolved: "gray",
    closed: "gray",
    paused: "yellow",
    disconnected: "gray",
    inactive: "gray",
    disabled: "gray",
  }[s] as never) || "gray";
