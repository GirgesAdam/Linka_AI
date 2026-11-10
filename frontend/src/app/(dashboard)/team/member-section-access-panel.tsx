"use client";

import { useEffect, useState, useTransition } from "react";

import { emptyMemberSectionAccess } from "@/lib/section-access";
import type { MemberSectionAccess, WorkspaceMember } from "@/lib/types";
import { getMemberSectionAccess, saveMemberSectionAccess } from "./actions";

const sectionOptions = [
  {
    key: "clinic_operations",
    label: "تشغيل العيادة",
    description: "إدارة الأطباء والخدمات والمخزون.",
  },
  {
    key: "administration",
    label: "الإدارة",
    description: "التحليلات والمالية والأتمتة والإعدادات والنشاط.",
  },
] as const;

export function MemberSectionAccessPanel({ members }: { members: WorkspaceMember[] }) {
  const editable = members.filter((member) => member.role === "member" && member.is_active);
  const [selectedId, setSelectedId] = useState(editable[0]?.membership_id ?? "");
  const [sectionAccess, setSectionAccess] = useState<MemberSectionAccess>(emptyMemberSectionAccess);
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
    // Prevent saving a stale editor while switching members.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setLoadedId(null);
    setError(null);
    setNotice(null);
    void getMemberSectionAccess(selectedId).then((response) => {
      if (!live) return;
      if (!response.data) {
        setError(response.error || "تعذر تحميل إعدادات الوصول.");
      } else {
        setSectionAccess(response.data.section_access);
        setVersion(response.data.permissions_version);
        setLegacy(response.data.is_legacy);
        setLoadedId(selectedId);
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
      const response = await saveMemberSectionAccess(selectedId, version, sectionAccess);
      if (!response.data) {
        setError(response.error || "تعذر حفظ إعدادات الوصول.");
        return;
      }
      setSectionAccess(response.data.section_access);
      setVersion(response.data.permissions_version);
      setLegacy(false);
      setNotice("تم حفظ إعدادات الوصول بنجاح.");
    });
  }

  return (
    <section className="mt-6 rounded-2xl border border-[var(--border)] bg-white p-5">
      <h2 className="text-base font-bold">إعدادات الوصول للأعضاء</h2>
      <p className="mt-1 text-xs leading-6 text-[var(--muted)]">
        يحدد المدير الأقسام التي يمكن لكل عضو الوصول إليها. باقي أقسام النظام متاحة تلقائيًا.
      </p>
      {!editable.length ? (
        <p className="mt-4 text-sm text-[var(--muted)]">لا يوجد أعضاء نشطون يمكن تعديل إعدادات الوصول لهم.</p>
      ) : (
        <>
          <label htmlFor="member-for-section-access" className="mt-5 block text-sm font-semibold">
            اختر عضوًا
          </label>
          <select
            id="member-for-section-access"
            value={selectedId}
            onChange={(event) => setSelectedId(event.target.value)}
            disabled={pending || loading}
            className="mt-2 w-full max-w-lg rounded-lg border border-[var(--border)] bg-white px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-60"
          >
            {editable.map((member) => (
              <option key={member.membership_id} value={member.membership_id}>
                {member.full_name || member.email}
              </option>
            ))}
          </select>
          {loading && <p className="mt-4 text-sm" role="status">جارٍ تحميل إعدادات الوصول...</p>}
          {error && <p className="mt-4 text-sm text-red-700" role="alert">{error}</p>}
          {notice && <p className="mt-4 text-sm text-emerald-700" role="status">{notice}</p>}
          {loadedId === selectedId && !loading && (
            <>
              {legacy && (
                <p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm leading-6 text-amber-900">
                  هذه العضوية تستخدم صلاحيات قديمة. لم يتم تغييرها تلقائيًا. اختر القسمين واحفظ الإعدادات لتحويلها إلى النظام الجديد.
                </p>
              )}
              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                {sectionOptions.map((section) => {
                  const enabled = sectionAccess[section.key];
                  return (
                    <label
                      key={section.key}
                      className="flex min-h-24 cursor-pointer items-start justify-between gap-4 rounded-xl border border-[var(--border)] p-4"
                    >
                      <span className="min-w-0">
                        <span className="block text-sm font-bold">{section.label}</span>
                        <span className="mt-1 block text-xs leading-5 text-[var(--muted)]">
                          {section.description}
                        </span>
                        <span className={"mt-2 inline-flex rounded-full px-2 py-1 text-[11px] font-bold " + (enabled ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-600")}>
                          {enabled ? "مسموح" : "مغلق"}
                        </span>
                      </span>
                      <input
                        type="checkbox"
                        aria-label={section.label}
                        checked={enabled}
                        disabled={pending}
                        onChange={(event) => {
                          setNotice(null);
                          setSectionAccess((current) => ({
                            ...current,
                            [section.key]: event.target.checked,
                          }));
                        }}
                        className="mt-1 size-4 accent-[var(--accent)]"
                      />
                    </label>
                  );
                })}
              </div>
              <div className="mt-4 flex items-center gap-3">
                <button
                  type="button"
                  onClick={save}
                  disabled={pending || loading}
                  className="rounded-lg bg-slate-900 px-5 py-2 text-sm font-bold text-white disabled:opacity-50"
                >
                  {pending ? "جارٍ الحفظ..." : "حفظ الإعدادات"}
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
