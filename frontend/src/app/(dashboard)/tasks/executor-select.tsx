"use client";

import { useState, useTransition } from "react";
import { CheckCircle2 } from "lucide-react";
import { initialActionFeedbackState } from "@/lib/action-feedback";

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
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="flex flex-col gap-1">
    <select
      aria-label="المسؤول عن المتابعة"
      value={selected}
      disabled={pending}
      onChange={(event) => {
        const nextValue = event.target.value;
        const previous = selected;
        setSelected(nextValue);
        setFeedback(null);
        setError(null);
        startTransition(async () => {
          const formData = new FormData();
          formData.set("task_id", taskId);
          formData.set("patient_id", patientId);
          formData.set("executor", nextValue);
          try {
            const result = await assignTask(initialActionFeedbackState, formData);
            if (result.error) {
              setSelected(previous);
              setError(result.error);
            } else {
              setFeedback(result.notice);
            }
          } catch (error) {
            setSelected(previous);
            setError(error instanceof Error ? error.message : "تعذر تحديث مسؤول المتابعة.");
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
    {error ? <span role="alert" className="text-xs font-semibold text-red-700">{error}</span> : feedback ? <span role="status" className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-700"><CheckCircle2 size={13} />{feedback}</span> : null}
    </div>
  );
}
