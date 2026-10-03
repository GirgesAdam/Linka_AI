"use server";

import { revalidatePath } from "next/cache";

import { TiaApiError, tiaRequest } from "@/lib/tia/api";

export type InviteMemberState = {
  notice: string | null;
  error: string | null;
};

type InvitationResult = {
  invitation_sent: boolean;
};

function inviteError(error: unknown): InviteMemberState {
  if (error instanceof TiaApiError) {
    if (error.status === 409) {
      return { notice: null, error: "العضو موجود بالفعل داخل فريق العيادة." };
    }
    return { notice: null, error: error.message };
  }
  if (error instanceof Error) return { notice: null, error: error.message };
  return { notice: null, error: "تعذر إرسال الدعوة. حاول مرة أخرى." };
}

export async function inviteMember(
  _previousState: InviteMemberState,
  formData: FormData,
): Promise<InviteMemberState> {
  const email = String(formData.get("email") || "").trim();
  const role = String(formData.get("role") || "member");
  if (!email) return { notice: null, error: "اكتب بريد العضو." };
  if (!["member", "admin"].includes(role)) {
    return { notice: null, error: "اختار صلاحية صحيحة للعضو." };
  }

  try {
    const result = await tiaRequest<InvitationResult>("/auth/workspace/invitations", {
      method: "POST",
      body: JSON.stringify({ email, role }),
    });
    revalidatePath("/team");
    return {
      notice: result.invitation_sent
        ? "تم إرسال الدعوة للعضو بنجاح."
        : "تمت إضافة العضو للفريق بنجاح.",
      error: null,
    };
  } catch (error) {
    return inviteError(error);
  }
}

export async function changeRole(formData: FormData) {
  const id = String(formData.get("membership_id"));
  const role = String(formData.get("role"));
  await tiaRequest(`/auth/workspace/members/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });
  revalidatePath("/team");
}
