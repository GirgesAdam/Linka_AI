import Link from "next/link";
import { ChevronLeft, Phone, Search, UsersRound } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatDateTime } from "@/lib/format";
import { labelForSource, labelForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import type { ClinicLaserDevice, Patient, Service, ServicePackageOffer } from "@/lib/types";
import { PatientRegistrationForm } from "./patient-registration-form";
import { patientDisplayName } from "./patient-utils";

function statusDescription(patient: Patient) {
  if (patient.status === "active") return "نشط";
  if (patient.status === "inactive") return "غير نشط";
  if (patient.status === "blocked") return "محظور";
  return labelForStatus(patient.status);
}

export default async function PatientsPage({ searchParams }: { searchParams: Promise<{ q?: string }> }) {
  const { q = "" } = await searchParams;
  const query = new URLSearchParams({ limit: "100" });
  if (q) query.set("q", q);
  const hasActiveFilters = Boolean(q);
  const [patients, packageOffers, laserDevices, services] = await Promise.all([
    tiaRequest<Patient[]>(`/crm/patients?${query.toString()}`),
    tiaRequest<ServicePackageOffer[]>("/booking/package-offers?active_only=true").catch(() => [] as ServicePackageOffer[]),
    tiaRequest<ClinicLaserDevice[]>("/inventory/laser-devices").catch(() => [] as ClinicLaserDevice[]),
    tiaRequest<Service[]>("/clinic/services").catch(() => [] as Service[]),
  ]);

  return (
    <>
      <PageHeader title="العملاء" description="ابحث عن أي عميل وافتح ملفه لمراجعة بيانات التواصل والمواعيد والمتابعات." />

      <div className="mb-4 flex max-w-3xl flex-wrap items-center gap-2">
        <form className="flex min-w-[260px] flex-1 gap-2">
          <div className="relative flex-1">
            <Search className="absolute right-3 top-3.5 text-slate-400" size={16} />
            <Input name="q" defaultValue={q} placeholder="ابحث بالاسم أو رقم الهاتف" className="pr-9" />
          </div>
          <Button type="submit" className="px-5">بحث</Button>
        </form>
        <PatientRegistrationForm
          offers={packageOffers.filter((offer) => offer.is_active)}
          devices={laserDevices.filter((device) => device.is_active)}
          services={services.filter((service) => service.is_active)}
        />
      </div>

      <Card>
        <CardContent className="p-0 sm:p-0">
          {patients.length ? (
            <>
              <div className="divide-y divide-[var(--border)] md:hidden">
                {patients.map((patient) => (
                  <Link key={patient.id} href={`/patients/${patient.id}`} className="interactive-row block p-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-black text-slate-950">{patientDisplayName(patient)}</div>
                        <div className="mt-1 inline-flex items-center gap-1.5 text-xs text-[var(--muted)]">
                          <Phone size={13} />
                          <span dir="ltr">{patient.phone || "بدون رقم هاتف"}</span>
                        </div>
                      </div>
                      <StatusBadge domain="patient" status={patient.status} label={statusDescription(patient)} showIcon={false} />
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-3 rounded-xl bg-slate-50 p-3 text-xs">
                      <div><div className="text-[var(--muted)]">المصدر</div><div className="mt-1 font-bold text-slate-800">{labelForSource(patient.source)}</div></div>
                      <div><div className="text-[var(--muted)]">آخر تواصل</div><div className="mt-1 font-bold text-slate-800">{formatDateTime(patient.last_contact_at)}</div></div>
                    </div>
                    <div className="mt-3 flex items-center justify-end text-xs text-[var(--muted)]">
                      <span className="inline-flex items-center gap-1 font-bold text-[var(--interactive)]">فتح الملف <ChevronLeft size={13} /></span>
                    </div>
                  </Link>
                ))}
              </div>

              <div className="table-shell hidden md:block">
                <table className="data-table min-w-[700px]">
                  <thead>
                    <tr>
                      <th>العميل</th>
                      <th>رقم الهاتف</th>
                      <th>مصدر العميل</th>
                      <th>الحالة</th>
                      <th>آخر تواصل</th>
                    </tr>
                  </thead>
                  <tbody>
                    {patients.map((patient) => (
                      <tr key={patient.id}>
                        <td className="font-bold">
                          <Link href={`/patients/${patient.id}`} className="interactive-link font-semibold text-[var(--interactive-strong)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]">{patientDisplayName(patient)}</Link>
                        </td>
                        <td dir="ltr" className="text-right">{patient.phone || "—"}</td>
                        <td>{labelForSource(patient.source)}</td>
                        <td><StatusBadge domain="patient" status={patient.status} label={statusDescription(patient)} showIcon={false} /></td>
                        <td>{formatDateTime(patient.last_contact_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <EmptyState
              icon={UsersRound}
              title={hasActiveFilters ? "لا توجد نتائج مطابقة" : "لا يوجد عملاء بعد"}
              description={hasActiveFilters ? "جرّب تعديل البحث أو الفلاتر الحالية." : "سيظهر العملاء هنا بعد إضافتهم أو استيراد بيانات العيادة."}
              action={hasActiveFilters ? <Link href="/patients" className={buttonVariants({ variant: "outline" })}>عرض كل العملاء</Link> : undefined}
            />
          )}
        </CardContent>
      </Card>
    </>
  );
}
