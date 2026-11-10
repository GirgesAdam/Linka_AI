"use server";

import { revalidatePath } from "next/cache";

import { TiaApiError, tiaRequest } from "@/lib/tia/api";
import { actionFeedbackFailure, type ActionFeedbackState } from "@/lib/action-feedback";
import type { MemberSectionAccess } from "@/lib/types";

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

export async function changeRole(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const id = String(formData.get("membership_id") || "").trim();
    const role = String(formData.get("role") || "");
    if (!id) throw new Error("تعذر تحديد العضو المطلوب تعديله.");
    if (!["admin", "member"].includes(role)) throw new Error("اختار صلاحية صحيحة للعضو.");
    await tiaRequest("/auth/workspace/members/" + id, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    });
    revalidatePath("/team");
    return { notice: "تم تحديث صلاحية العضو بنجاح.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تحديث صلاحية العضو. حاول مرة أخرى.");
  }
}

type MemberSectionAccessResponse = {
  membership_id: string;
  section_access: MemberSectionAccess;
  permissions_version: number;
  is_legacy: boolean;
};

export async function getMemberSectionAccess(
  membershipId: string,
): Promise<{ data: MemberSectionAccessResponse | null; error: string | null }> {
  try {
    const { getAppContext } = await import("@/lib/tia/workspace");
    const { workspace } = await getAppContext();
    if (workspace.role !== "admin") return { data: null, error: "هذه الإعدادات متاحة للمدير فقط." };
    const data = await tiaRequest<MemberSectionAccessResponse>(
      "/auth/workspace/members/" + encodeURIComponent(membershipId) + "/section-access",
    );
    return { data, error: null };
  } catch (error) {
    return {
      data: null,
      error: error instanceof Error ? error.message : "تعذر تحميل إعدادات الوصول.",
    };
  }
}

export async function saveMemberSectionAccess(
  membershipId: string,
  expectedVersion: number,
  sectionAccess: MemberSectionAccess,
): Promise<{ data: MemberSectionAccessResponse | null; error: string | null }> {
  try {
    const { getAppContext } = await import("@/lib/tia/workspace");
    const { workspace } = await getAppContext();
    if (workspace.role !== "admin") return { data: null, error: "هذه العملية متاحة للمدير فقط." };
    const data = await tiaRequest<MemberSectionAccessResponse>(
      "/auth/workspace/members/" + encodeURIComponent(membershipId) + "/section-access",
      {
        method: "PUT",
        body: JSON.stringify({ expected_version: expectedVersion, section_access: sectionAccess }),
      },
    );
    revalidatePath("/team");
    return { data, error: null };
  } catch (error) {
    if (error instanceof TiaApiError && error.status === 409) {
      return { data: null, error: "تم تغيير إعدادات الوصول من مكان آخر. أعد تحميلها قبل الحفظ." };
    }
    return {
      data: null,
      error: error instanceof Error ? error.message : "تعذر حفظ إعدادات الوصول.",
    };
  }
}
