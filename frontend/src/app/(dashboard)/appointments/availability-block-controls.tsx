"use client";

import { useActionState, useRef } from "react";
import { Ban, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createAvailabilityBlock, type AvailabilityBlockActionState } from "./actions";

const initialState: AvailabilityBlockActionState = { ok: false, message: "" };

export function AvailabilityBlockControls({ branchId, date }: { branchId: string; date: string }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [state, action, pending] = useActionState(createAvailabilityBlock, initialState);
  return <>
    <Button type="button" variant="outline" onClick={() => dialogRef.current?.showModal()}><Ban size={16} /> قفل فترة</Button>
    <dialog ref={dialogRef} className="m-auto w-[min(92vw,30rem)] rounded-2xl border border-slate-200 bg-white p-0 shadow-2xl backdrop:bg-slate-950/30">
      <form action={action} className="p-5">
        <input type="hidden" name="branch_id" value={branchId} />
        <div className="flex items-start justify-between gap-3"><div><div className="text-base font-black text-slate-950">قفل فترة</div><div className="mt-1 text-xs font-semibold text-slate-500">امنع أي حجز جديد في الفرع خلال الفترة دي.</div></div><button type="button" aria-label="إغلاق" onClick={() => dialogRef.current?.close()} className="rounded-lg p-2 text-slate-500 hover:bg-slate-100"><X size={17} /></button></div>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <label className="text-xs font-bold text-slate-700 sm:col-span-2">التاريخ<Input className="mt-1.5" name="date" type="date" defaultValue={date} required /></label>
          <label className="text-xs font-bold text-slate-700">من<Input className="mt-1.5" name="start_time" type="time" required /></label>
          <label className="text-xs font-bold text-slate-700">إلى<Input className="mt-1.5" name="end_time" type="time" required /></label>
          <label className="text-xs font-bold text-slate-700 sm:col-span-2">السبب <span className="font-semibold text-slate-400">(اختياري)</span><Input className="mt-1.5" name="reason" maxLength={500} /></label>
        </div>
        {state.message && <div className={`mt-4 rounded-xl px-3 py-2 text-xs font-bold ${state.ok ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"}`}>{state.message}</div>}
        <div className="mt-5 flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => dialogRef.current?.close()}>إلغاء</Button><Button type="submit" disabled={pending}>{pending ? "جارٍ الحفظ..." : "قفل الفترة"}</Button></div>
      </form>
    </dialog>
  </>;
}
