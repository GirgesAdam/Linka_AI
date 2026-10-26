"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { updatePatientProfileAction } from "../actions";
import { patientSourceOptions } from "../patient-utils";

export function PatientProfileEditor({
  patientId,
  name: initialName,
  phone: initialPhone,
  customerId: initialCustomerId,
  source: initialSource,
}: {
  patientId: string;
  name: string;
  phone: string | null;
  customerId: string | null;
  source: string;
}) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(initialName);
  const [phone, setPhone] = useState(initialPhone || "");
  const [customerId, setCustomerId] = useState(initialCustomerId || "");
  const [source, setSource] = useState(initialSource);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function cancel() {
    setName(initialName);
    setPhone(initialPhone || "");
    setCustomerId(initialCustomerId || "");
    setSource(initialSource);
    setError(null);
    setEditing(false);
  }

  async function save() {
    const normalizedName = name.trim();
    if (!normalizedName) {
      setError("أدخل اسم العميل.");
      return;
    }
    const changes: {
      name?: string;
      phone?: string | null;
      customer_id?: string | null;
      source?: string;
    } = {};
    if (normalizedName !== initialName) changes.name = normalizedName;
    if (phone.trim() !== (initialPhone || "")) changes.phone = phone.trim() || null;
    if (customerId.trim() !== (initialCustomerId || "")) changes.customer_id = customerId.trim() || null;
    if (source !== initialSource) changes.source = source;

    setSaving(true);
    setError(null);
    const result = await updatePatientProfileAction(patientId, changes);
    setSaving(false);
    if (!result.ok) {
      setError(result.error || "تعذر تحديث بيانات العميل.");
      return;
    }
    setEditing(false);
    router.refresh();
  }

  if (!editing) {
    return <Button type="button" size="sm" variant="outline" onClick={() => setEditing(true)}>تعديل</Button>;
  }

  return (
    <div className="mt-4 space-y-3 rounded-xl border border-[var(--border)] bg-[var(--surface-2)] p-3">
      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm font-medium text-red-800">{error}</p>}
      <label className="grid gap-1.5 text-xs font-bold text-slate-700">
        الاسم
        <Input value={name} onChange={(event) => setName(event.target.value)} maxLength={120} autoComplete="name" />
      </label>
      <label className="grid gap-1.5 text-xs font-bold text-slate-700">
        رقم الموبايل
        <Input value={phone} onChange={(event) => setPhone(event.target.value)} type="tel" dir="ltr" autoComplete="tel" />
      </label>
      <label className="grid gap-1.5 text-xs font-bold text-slate-700">
        ID
        <Input value={customerId} onChange={(event) => setCustomerId(event.target.value)} dir="ltr" autoComplete="off" />
      </label>
      <label className="grid gap-1.5 text-xs font-bold text-slate-700">
        مصدر العميل
        <select value={source} onChange={(event) => setSource(event.target.value)} className="form-control h-10 min-h-10">
          {patientSourceOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <div className="flex justify-end gap-2">
        <Button type="button" size="sm" variant="ghost" onClick={cancel} disabled={saving}>إلغاء</Button>
        <Button type="button" size="sm" onClick={save} disabled={saving}>{saving ? "جارٍ الحفظ..." : "حفظ"}</Button>
      </div>
    </div>
  );
}
