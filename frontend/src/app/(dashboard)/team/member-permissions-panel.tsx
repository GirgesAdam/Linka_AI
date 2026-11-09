"use client";

import { useEffect, useState, useTransition } from "react";
import {
  PAGE_PERMISSION_KEYS,
  emptyMemberPermissions,
  type PagePermissionKey,
  type PagePermissionLevel,
} from "@/lib/page-permissions";
import type { WorkspaceMember } from "@/lib/types";
import { getMemberPermissions, saveMemberPermissions } from "./actions";

const pageLabels: Record<PagePermissionKey, string> = {
  dashboard: "الرئيسية",
  "inbox.list": "صندوق الوارد",
  "inbox.detail": "تفاصيل المحادثة",
  "appointments.calendar": "تقويم المواعيد",
  "appointments.detail": "تفاصيل الموعد",
  "appointments.reschedule": "إعادة الجدولة",
  "patients.list": "قائمة العملاء",
  "patients.detail": "ملف العميل",
  tasks: "المهام والمتابعات",
  doctors: "الأطباء",
  services: "الخدمات",
  inventory: "المخزن",
  "analytics.overview": "نظرة عامة على التحليلات",
  "analytics.campaigns": "الحملات",
  "analytics.campaign_detail": "تفاصيل الحملة",
  "analytics.cohort_detail": "تفاصيل الشريحة",
  "analytics.outstanding_balances": "المبالغ المستحقة",
  finance: "المالية",
  automations: "الأتمتة",
  activity: "سجل النشاط",
  team: "إدارة الفريق (مدير فقط)",
  setup: "إعدادات العيادة",
  "setup.whatsapp": "إعداد واتساب",
  "demo.home": "الصفحة التجريبية",
  "demo.agent": "تجربة المساعد",
};

type ReadResponse = {
  page_permissions: Record<string, PagePermissionLevel> | null;
  permissions_version: number;
  is_legacy: boolean;
};

function toEditorMatrix(data: ReadResponse): Record<PagePermissionKey, PagePermissionLevel> {
  const result = emptyMemberPermissions();
  if (data.page_permissions) {
    for (const [key, level] of Object.entries(data.page_permissions)) {
      if (!PAGE_PERMISSION_KEYS.includes(key as PagePermissionKey)) {
        throw new Error("بيانات الصلاحيات المحفوظة غير صالحة. يلزم مراجعتها.");
      }
      if (level !== "none" && level !== "manage") {
        throw new Error("مستوى صلاحية غير معروف. يلزم مراجعة البيانات.");
      }
      result[key as PagePermissionKey] = level;
    }
  }
  if (result.team !== "none") throw new Error("إدارة الفريق يجب أن تظل متاحة للمدير فقط.");
  return result;
}

export function MemberPermissionsPanel({ members }: { members: WorkspaceMember[] }) {
  const editable = members.filter((member) => member.role === "member" && member.is_active);
  const [selectedId, setSelectedId] = useState(editable[0]?.membership_id ?? "");
  const [permissions, setPermissions] = useState(emptyMemberPermissions);
  const [version, setVersion] = useState(0);
  const [legacy, setLegacy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [pending, startTransition] = useTransition();
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadedId, setLoadedId] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedId) return;
    let live = true;
    // Do not allow saving a stale editor while switching between members.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setLoadedId(null);
    setError(null);
    setNotice(null);
    void getMemberPermissions(selectedId).then((response) => {
      if (!live) return;
      if (!response.data) {
        setError(response.error || "تعذر تحميل الصلاحيات.");
      } else {
        try {
          setPermissions(toEditorMatrix(response.data));
          setVersion(response.data.permissions_version);
          setLegacy(response.data.is_legacy);
          setLoadedId(selectedId);
        } catch (loadError) {
          setError(loadError instanceof Error ? loadError.message : "تعذر قراءة الصلاحيات.");
        }
      }
      setLoading(false);
    });
    return () => { live = false; };
  }, [selectedId]);

  function save() {
    if (!selectedId || selectedId !== loadedId || pending || loading) return;
    setError(null);
    setNotice(null);
    startTransition(async () => {
      const response = await saveMemberPermissions(selectedId, version, permissions);
      if (!response.data) {
        setError(response.error || "تعذر حفظ الصلاحيات.");
        return;
      }
      setPermissions(toEditorMatrix(response.data));
      setVersion(response.data.permissions_version);
      setLegacy(false);
      setNotice("تم حفظ صلاحيات العضو بنجاح.");
    });
  }

  return (
    <section className="mt-6 rounded-2xl border border-[var(--border)] bg-white p-5">
      <h2 className="text-base font-bold">صلاحيات صفحات أعضاء الفريق</h2>
      <p className="mt-1 text-xs leading-6 text-[var(--muted)]">
        شغّل الصفحات التي يستطيع العضو إدارتها. عند الإيقاف تختفي الصفحة ويتم رفض الوصول المباشر إليها.
        تظل قواعد ملكية المهام والمحادثات سارية.
      </p>
      {!editable.length ? (
        <p className="mt-4 text-sm text-[var(--muted)]">لا يوجد أعضاء نشطون لتعديل صلاحياتهم.</p>
      ) : (
        <>
          <label htmlFor="member-for-permissions" className="mt-5 block text-sm font-semibold">
            اختر عضو الفريق
          </label>
          <select
            id="member-for-permissions"
            value={selectedId}
            onChange={(event) => setSelectedId(event.target.value)}
            className="mt-2 w-full max-w-lg rounded-lg border border-[var(--border)] bg-white px-3 py-2 text-sm"
          >
            {editable.map((member) => (
              <option key={member.membership_id} value={member.membership_id}>
                {member.full_name || member.email}
              </option>
            ))}
          </select>
          {loading && <p className="mt-4 text-sm" role="status">جارٍ تحميل الصلاحيات...</p>}
          {error && <p className="mt-4 text-sm text-red-700" role="alert">{error}</p>}
          {notice && <p className="mt-4 text-sm text-emerald-700" role="status">{notice}</p>}
          {loadedId === selectedId && !loading && (
            <>
              {legacy && (
                <p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                  العضو يستخدم حالياً الصلاحيات القديمة. عند الحفظ ستصبح الصفحات غير المحددة ممنوعة.
                </p>
              )}
              <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                {PAGE_PERMISSION_KEYS.map((key) => (
                  <label key={key} className="flex min-h-12 items-center justify-between gap-3 rounded-lg border border-[var(--border)] px-3 py-2 text-sm">
                    <span className="font-semibold">{pageLabels[key]}</span>
                    <input
                      type="checkbox"
                      aria-label={pageLabels[key]}
                      checked={permissions[key] === "manage"}
                      disabled={key === "team" || pending}
                      onChange={(event) => {
                        setNotice(null);
                        setPermissions((current) => ({
                          ...current,
                          [key]: event.target.checked ? "manage" : "none",
                        }));
                      }}
                      className="size-4 accent-[var(--accent)]"
                    />
                  </label>
                ))}
              </div>
              <div className="mt-4 flex items-center gap-3">
                <button
                  type="button"
                  onClick={save}
                  disabled={pending}
                  className="rounded-lg bg-slate-900 px-5 py-2 text-sm font-bold text-white disabled:opacity-50"
                >
                  {pending ? "جارٍ الحفظ..." : "حفظ الصلاحيات"}
                </button>
                <span className="text-xs text-[var(--muted)]">الإصدار: {version}</span>
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}
