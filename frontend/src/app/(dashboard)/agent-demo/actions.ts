"use server";

import { revalidatePath } from "next/cache";

import { tiaRequest } from "@/lib/tia/api";
import { initialAgentDemoState, normalizeAgentDemoState, type AgentDemoState } from "./state";

interface AgentChatResponse {
  conversation_id: string;
  reply: string | null;
  model: string | null;
  handoff_required: boolean;
  agent_paused: boolean;
}

export async function runAgentDemoAction(
  previousState: AgentDemoState,
  formData: FormData,
): Promise<AgentDemoState> {
  const safePreviousState = normalizeAgentDemoState(previousState);
  const mode = String(formData.get("mode") || "send");
  const patientId = String(formData.get("patient_id") || "").trim();

  if (mode === "reset") {
    return { ...initialAgentDemoState, patientId: patientId || safePreviousState.patientId };
  }

  const message = String(formData.get("message") || "").trim();
  if (!patientId) return { ...safePreviousState, error: "اختر عميل Demo الأول." };
  if (!message) return { ...safePreviousState, error: "اكتب رسالة للـAgent." };

  const samePatient = safePreviousState.patientId === patientId;
  const conversationId = samePatient ? safePreviousState.conversationId : null;
  const history = samePatient ? safePreviousState.messages : [];

  try {
    const response = await tiaRequest<AgentChatResponse>("/agent/chat", {
      method: "POST",
      body: JSON.stringify({
        patient_id: patientId,
        conversation_id: conversationId,
        channel: "web",
        message,
      }),
    });

    revalidatePath("/dashboard");
    revalidatePath("/appointments");
    revalidatePath(`/patients/${patientId}`);
    revalidatePath("/inbox");

    const reply = response.reply || (response.handoff_required
      ? "Linka نقلت المحادثة للفريق البشري."
      : "تم تنفيذ الدور بدون رد نصي.");

    return {
      patientId,
      conversationId: response.conversation_id,
      messages: [...history, { role: "patient", content: message }, { role: "ai", content: reply }],
      model: response.model,
      error: null,
    };
  } catch (error) {
    return {
      patientId,
      conversationId,
      messages: history,
      model: safePreviousState.model,
      error: error instanceof Error ? error.message : "تعذر تشغيل Linka الآن.",
    };
  }
}
