"use client";

import { useState, useTransition } from "react";

import { assignTask } from "./actions";

type MemberOption = { user_id: string; label: string };

export function ExecutorSelect({
  taskId,
  patientId,
  value,
  allowTia,
  members,
}: {
  taskId: string;
  patientId: string;
  value: string;
  allowTia: boolean;
  members: MemberOption[];
}) {
  const [selected, setSelected] = useState(value);
  const [pending, startTransition] = useTransition();

  return (
    <select
      aria-label="المسؤول عن المتابعة"
      value={selected}
      disabled={pending}
      onChange={(event) => {
        const nextValue = event.target.value;
        const previous = selected;
        setSelected(nextValue);
        startTransition(async () => {
          const formData = new FormData();
          formData.set("task_id", taskId);
          formData.set("patient_id", patientId);
          formData.set("executor", nextValue);
          try {
            await assignTask(formData);
          } catch (error) {
            setSelected(previous);
            throw error;
          }
        });
      }}
      className="h-8 max-w-48 rounded-lg border border-slate-200 bg-white px-2 text-xs font-semibold text-slate-700 disabled:cursor-wait disabled:opacity-60"
    >
      {allowTia && <option value="tia">Linka</option>}
      <option value="unassigned">غير مسندة</option>
      {members.map((member) => (
        <option key={member.user_id} value={`staff:${member.user_id}`}>{member.label}</option>
      ))}
    </select>
  );
}
