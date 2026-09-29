"use server";

import { revalidatePath } from "next/cache";
import { tiaRequest } from "@/lib/tia/api";
import type { KnowledgeAssistantState, KnowledgeEditProposal } from "@/lib/agent-knowledge-types";

export async function knowledgeAssistantAction(
  previousState: KnowledgeAssistantState,
  formData: FormData,
): Promise<KnowledgeAssistantState> {
  const mode = String(formData.get("mode") || "propose");
  try {
    if (mode === "apply") {
      const raw = String(formData.get("proposal") || "");
      if (!raw) throw new Error("مفيش تعديل مقترح للتنفيذ.");
      const proposal = JSON.parse(raw) as KnowledgeEditProposal;
      await tiaRequest("/clinic/knowledge/ai/apply", {
        method: "POST",
        body: JSON.stringify({ base_fingerprint: proposal.base_fingerprint, actions: proposal.actions }),
      });
      revalidatePath("/knowledge");
      revalidatePath("/setup");
      return { proposal: null, notice: "تم تطبيق التعديل. الجداول اتحدثت ببيانات الـAgent الحالية.", error: null };
    }

    const message = String(formData.get("message") || "").trim();
    if (!message) throw new Error("اكتب التعديل اللي عايز Linka تساعدك فيه.");
    const proposal = await tiaRequest<KnowledgeEditProposal>("/clinic/knowledge/ai/propose", {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    return { proposal, notice: null, error: null };
  } catch (error) {
    return {
      proposal: previousState.proposal,
      notice: null,
      error: error instanceof Error ? error.message : "حصل خطأ أثناء تجهيز التعديل.",
    };
  }
}
