"use client";

import { useActionState, useEffect } from "react";
import { Bot, CheckCircle2, LoaderCircle, Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import type { KnowledgeAssistantState } from "@/lib/agent-knowledge-types";
import { knowledgeAssistantAction } from "./actions";

const initialKnowledgeAssistantState: KnowledgeAssistantState = { proposal: null, notice: null, error: null };

export function AgentKnowledgeAssistant({ admin }: { admin: boolean }) {
  const router = useRouter();
  const [state, action, pending] = useActionState(knowledgeAssistantAction, initialKnowledgeAssistantState);

  useEffect(() => {
    if (state.notice) router.refresh();
  }, [router, state.notice]);

  if (!admin) return null;
  const proposal = state.proposal;

  return (
    <section className="rounded-3xl border border-teal-200 bg-gradient-to-br from-teal-50 to-white p-5">
      <div className="flex items-start gap-3">
        <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-teal-700 text-white"><Bot size={21} /></span>
        <div>
          <div className="flex items-center gap-2"><h2 className="text-lg font-black">عدّل بيانات العيادة مع Linka</h2><Sparkles size={16} className="text-teal-700" /></div>
          <p className="mt-1 text-sm text-slate-600">اكتب التعديل بطريقتك. Linka هتفهمه وتعرض عليك التغيير قبل ما يتنفذ.</p>
        </div>
      </div>

      <form action={action} className="mt-5 space-y-3">
        <textarea name="message" rows={3} className="w-full rounded-2xl border border-[var(--border)] bg-white p-3 text-sm" placeholder="مثال: خلي مواعيد فرع مدينة نصر من السبت للخميس من 10 الصبح لـ10 بالليل والجمعة إجازة" />
        <Button type="submit" name="mode" value="propose" disabled={pending}>{pending ? <LoaderCircle className="animate-spin" size={18} /> : <Bot size={18} />} اسأل Linka</Button>
      </form>

      {proposal && (
        <div className="mt-4 rounded-2xl border border-teal-100 bg-white p-4">
          <div className="text-xs font-black text-teal-700">Linka</div>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-7">{proposal.assistant_message}</p>
          {proposal.preview_lines.length > 0 && (
            <div className="mt-4 space-y-2">
              {proposal.preview_lines.map((line, index) => <div key={index} className="rounded-xl bg-slate-50 px-3 py-2 text-sm">{line}</div>)}
            </div>
          )}
          {proposal.clarification_question && <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">{proposal.clarification_question}</div>}
          {proposal.requires_confirmation && (
            <form action={action} className="mt-4">
              <input type="hidden" name="proposal" value={JSON.stringify(proposal)} />
              <Button type="submit" name="mode" value="apply" disabled={pending}><CheckCircle2 size={18} /> تأكيد وتنفيذ التعديل</Button>
            </form>
          )}
        </div>
      )}

      {state.notice && <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm font-semibold text-emerald-800">{state.notice}</div>}
      {state.error && <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{state.error}</div>}
    </section>
  );
}
