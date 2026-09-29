import Link from "next/link";
import { ChevronLeft, Phone, Search, UsersRound } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatDateTime } from "@/lib/format";
import { labelForSource, labelForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import type { Patient } from "@/lib/types";

function statusDescription(patient: Patient) {
  if (patient.status === "active") return "نشط";
  if (patient.status === "inactive") return "غير نشط";
  if (patient.status === "blocked") return "محظور";
  return labelForStatus(patient.status);
}

export default async function PatientsPage({ searchParams }: { searchParams: Promise<{ q?: string; status?: string; source?: string }> }) {
  const { q = "", status = "", source = "" } = await searchParams;
  const query = new URLSearchParams({ limit: "100" });
  if (q) query.set("q", q);
  if (status) query.set("status", status);
  if (source) query.set("source", source);
  const patients = await tiaRequest<Patient[]>(`/crm/patients?${query.toString()}`);

  return (
    <>
      <PageHeader title="العملاء" description="ابحث عن أي عميل وافتح ملفه لمراجعة بيانات التواصل والمواعيد والمتابعات." />

      <div className="mb-4 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 text-xs font-semibold text-slate-600">
        حالة العميل هنا هي الحالة المسجلة في النظام. «نشط» لا تعني تلقائيًا أنه تواصل خلال آخر 3 شهور؛ آخر تواصل ظاهر في عمود منفصل.
      </div>

      <form className="mb-4 grid gap-2 md:grid-cols-[minmax(260px,1fr)_180px_190px_auto]">
        <div className="relative">
          <Search className="absolute right-3 top-3.5 text-slate-400" size={16} />
          <Input name="q" defaultValue={q} placeholder="ابحث بالاسم أو رقم الهاتف" className="pr-9" />
        </div>
        <select name="status" defaultValue={status} className="form-control h-10 min-h-10" aria-label="حالة العميل">
          <option value="">كل الحالات</option><option value="active">نشط</option><option value="inactive">غير نشط</option><option value="blocked">محظور</option>
        </select>
        <select name="source" defaultValue={source} className="form-control h-10 min-h-10" aria-label="مصدر العميل">
          <option value="">كل المصادر</option><option value="whatsapp">WhatsApp</option><option value="instagram">Instagram</option><option value="facebook">Facebook</option><option value="website">الموقع</option><option value="referral">ترشيح</option><option value="walk_in">زيارة مباشرة</option><option value="campaign">حملة</option><option value="phone">هاتف</option><option value="other">أخرى</option>
        </select>
        <Button type="submit" className="px-5">تطبيق</Button>
      </form>

      <Card>
        <CardContent className="p-0 sm:p-0">
          {patients.length ? (
            <>
              <div className="divide-y divide-[var(--border)] md:hidden">
                {patients.map((patient) => (
                  <Link key={patient.id} href={`/patients/${patient.id}`} className="block p-4 transition hover:bg-slate-50">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-black text-slate-950">{patient.first_name} {patient.last_name || ""}</div>
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
                          <Link href={`/patients/${patient.id}`} className="text-[var(--interactive-strong)] hover:underline">{patient.first_name} {patient.last_name || ""}</Link>
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
              title={q ? "لا يوجد عميل مطابق للبحث" : "لا يوجد عملاء بعد"}
              description={q ? "جرّب البحث بجزء من الاسم أو رقم الهاتف." : "سيظهر العملاء هنا بعد إضافتهم أو استيراد بيانات العيادة."}
            />
          )}
        </CardContent>
      </Card>
    </>
  );
}
