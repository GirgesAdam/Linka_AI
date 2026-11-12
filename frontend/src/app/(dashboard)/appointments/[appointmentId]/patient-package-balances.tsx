import type { PatientPackage } from "@/lib/types";

const statusLabels: Record<string, string> = {
  active: "نشطة",
  exhausted: "مستنفدة",
  expired: "منتهية الصلاحية",
  cancelled: "ملغاة",
};

function packageBalances(packages: PatientPackage[]) {
  return [...packages].sort((a, b) => {
    const activeDifference = Number(b.effective_status === "active") - Number(a.effective_status === "active");
    return activeDifference || a.name.localeCompare(b.name, "ar");
  });
}

export function PatientPackageBalances({
  packages,
  loadFailed,
}: {
  packages: PatientPackage[];
  loadFailed: boolean;
}) {
  if (loadFailed) {
    return (
      <div role="alert" className="rounded-lg border border-red-200 bg-white p-3 text-xs font-bold text-red-700">
        تعذر تحميل أرصدة باكيدجات العميل. حدّث الصفحة للمحاولة مرة أخرى؛ الأرصدة غير متاحة حالياً.
      </div>
    );
  }

  if (packages.length === 0) {
    return <div className="rounded-lg bg-white p-3 text-xs text-[var(--muted)]">لا توجد باكيدجات مسجلة لهذا العميل.</div>;
  }

  return (
    <>
      {packageBalances(packages).map((pkg) => {
        const totalKnown = pkg.sessions_total_known;
        const previouslyUsed = totalKnown && pkg.opening_sessions_remaining !== null
          ? Math.max(pkg.sessions_purchased - pkg.opening_sessions_remaining, 0)
          : 0;
        const consumed = previouslyUsed + pkg.sessions_consumed;
        const number = (value: number) => value.toLocaleString("ar-EG");
        return (
          <div key={pkg.id} className="rounded-lg bg-white p-3" data-package-status={pkg.effective_status}>
            <b className="block text-sm text-slate-900">{pkg.name}</b>
            {pkg.laser_device_name || pkg.laser_device_key ? (
              <div className="mt-1 text-xs text-[var(--muted)]">
                الجهاز: {pkg.laser_device_name || pkg.laser_device_key}
              </div>
            ) : null}
            <div className="mt-1 text-xs text-[var(--muted)]">
              الحالة: {statusLabels[pkg.effective_status] ?? pkg.effective_status}
            </div>
            <div className="mt-2 grid gap-1 text-xs text-slate-700">
              <div>إجمالي الجلسات: {totalKnown ? number(pkg.sessions_purchased) : "غير محدد"}</div>
              <div>{totalKnown ? "تم استخدامها" : "مستخدمة منذ الاستيراد"}: {number(consumed)}</div>
              <div>محجوزة: {number(pkg.sessions_reserved)}</div>
              <b className="text-slate-900">المتبقي: {number(pkg.sessions_remaining)}</b>
            </div>
          </div>
        );
      })}
    </>
  );
}
