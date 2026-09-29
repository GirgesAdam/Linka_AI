"use client";

import Link from "next/link";
import { useActionState, useEffect, useRef, useState } from "react";
import { Bot, CalendarCheck2, LoaderCircle, RotateCcw, Send, UserRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { Patient } from "@/lib/types";
import { runAgentDemoAction } from "./actions";
import { initialAgentDemoState, normalizeAgentDemoState } from "./state";

const suggestions = [
  "عايزة أحجز Full Body Laser أقرب ميعاد مناسب بعد الساعة 5 مساءً",
  "عندي مواعيد جاية؟",
  "ممكن أغيّر ميعادي لأقرب وقت متاح؟",
  "عايزة ألغي الحجز الجاي",
] as const;

function patientName(patient: Patient) {
  return [patient.first_name, patient.last_name].filter(Boolean).join(" ");
}

export function AgentDemoPlayground({ patients }: { patients: Patient[] }) {
  const [state, action, pending] = useActionState(runAgentDemoAction, initialAgentDemoState);
  const currentState = normalizeAgentDemoState(state);
  const [message, setMessage] = useState("");
  const transcriptRef = useRef<HTMLDivElement>(null);
  const defaultPatientId = currentState.patientId || patients[0]?.id || "";

  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight, behavior: "smooth" });
  }, [currentState.messages.length]);

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1.3fr)_minmax(300px,.7fr)]">
      <section className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
        <div className="border-b border-slate-100 bg-gradient-to-l from-teal-50 to-white p-5">
          <div className="flex items-center gap-3">
            <span className="grid size-11 place-items-center rounded-2xl bg-teal-700 text-white"><Bot size={21} /></span>
            <div>
              <h2 className="font-black text-slate-950">محادثة عميل حقيقية مع Linka</h2>
              <p className="mt-1 text-xs leading-5 text-slate-500">نفس الـAgent ونفس booking tools المستخدمة في النظام — لكن داخل Demo معزولة.</p>
            </div>
          </div>
        </div>

        <div ref={transcriptRef} className="h-[430px] space-y-4 overflow-y-auto bg-slate-50/60 p-5">
          {!currentState.messages.length && (
            <div className="grid h-full place-items-center text-center">
              <div className="max-w-md">
                <Bot className="mx-auto text-teal-700" size={34} />
                <h3 className="mt-3 font-black">ابدأ كأنك عميل للعيادة</h3>
                <p className="mt-2 text-sm leading-6 text-slate-500">اطلب حجز، استفسر عن المواعيد، غيّر الحجز أو الغيه. بعد التنفيذ افتح المواعيد أو ملف العميل وتأكد من أثر العملية في قاعدة البيانات.</p>
              </div>
            </div>
          )}
          {currentState.messages.map((item, index) => (
            <div key={`${item.role}-${index}`} className={`flex ${item.role === "patient" ? "justify-start" : "justify-end"}`}>
              <div className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-7 ${item.role === "patient" ? "bg-white text-slate-900 shadow-sm" : "bg-teal-700 text-white"}`}>
                <div className={`mb-1 text-[10px] font-black ${item.role === "patient" ? "text-slate-400" : "text-teal-100"}`}>{item.role === "patient" ? "العميل" : "Linka"}</div>
                <div className="whitespace-pre-wrap">{item.content}</div>
              </div>
            </div>
          ))}
          {pending && <div className="flex justify-end"><div className="flex items-center gap-2 rounded-2xl bg-teal-700 px-4 py-3 text-sm text-white"><LoaderCircle className="animate-spin" size={16} />Linka بتنفذ الطلب...</div></div>}
        </div>

        <form action={action} className="border-t border-slate-100 p-4">
          <input type="hidden" name="patient_id" value={defaultPatientId} />
          <div className="flex gap-2">
            <textarea
              name="message"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={2}
              disabled={pending || !patients.length}
              className="min-h-12 flex-1 resize-none rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
              placeholder="مثال: عايزة أحجز Full Body Laser بكرة بعد 5..."
            />
            <Button type="submit" name="mode" value="send" disabled={pending || !message.trim() || !patients.length} className="self-end"><Send size={17} /> إرسال</Button>
          </div>
        </form>
      </section>

      <aside className="space-y-5">
        <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center gap-2 font-black"><UserRound size={18} className="text-teal-700" /> شخصية العميل</div>
          <form action={action} className="mt-4 space-y-3">
            <select name="patient_id" defaultValue={defaultPatientId} className="h-11 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm font-semibold">
              {patients.map((patient) => <option key={patient.id} value={patient.id}>{patientName(patient)}{patient.phone ? ` · ${patient.phone}` : ""}</option>)}
            </select>
            <Button type="submit" name="mode" value="reset" variant="outline" className="w-full" disabled={pending}><RotateCcw size={16} /> محادثة جديدة بهذا العميل</Button>
          </form>
          {currentState.patientId && (
            <div className="mt-3 grid grid-cols-2 gap-2">
              <Link href={`/patients/${currentState.patientId}`} className="rounded-xl border border-slate-200 px-3 py-2 text-center text-xs font-bold hover:bg-slate-50">ملف العميل</Link>
              <Link href="/appointments" className="rounded-xl border border-slate-200 px-3 py-2 text-center text-xs font-bold hover:bg-slate-50">المواعيد</Link>
            </div>
          )}
        </section>

        <section className="rounded-3xl border border-teal-200 bg-teal-50/70 p-5">
          <div className="flex items-center gap-2 font-black text-teal-950"><CalendarCheck2 size={18} /> اختبارات مقترحة</div>
          <div className="mt-3 space-y-2">
            {suggestions.map((suggestion) => (
              <button key={suggestion} type="button" onClick={() => setMessage(suggestion)} className="w-full rounded-xl border border-teal-100 bg-white px-3 py-2 text-right text-xs leading-5 text-slate-700 transition hover:border-teal-300">{suggestion}</button>
            ))}
          </div>
        </section>

        <section className="rounded-3xl border border-amber-200 bg-amber-50 p-4 text-xs leading-6 text-amber-950">
          <b>Demo Sandbox:</b> الحجوزات والتعديلات تُكتب فعلًا في قاعدة بيانات الـDemo. إرسال الرسائل إلى WhatsApp/Gmail/providers الخارجية متوقف بالكامل.
          {currentState.model && <div className="mt-2 text-[10px] text-amber-700">آخر model: {currentState.model}</div>}
        </section>

        {currentState.error && <div className="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">{currentState.error}</div>}
      </aside>
    </div>
  );
}
