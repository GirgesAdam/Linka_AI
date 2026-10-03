"use client";

import { useActionState } from "react";
import { CheckCircle2, LoaderCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { inviteMember, type InviteMemberState } from "./actions";

const initialInviteMemberState: InviteMemberState = {
  notice: null,
  error: null,
};

export function InviteMemberForm() {
  const [state, action, pending] = useActionState(
    inviteMember,
    initialInviteMemberState,
  );

  return (
    <form action={action} className="space-y-3">
      <Input
        name="email"
        type="email"
        placeholder="member@clinic.com"
        required
        dir="ltr"
        autoComplete="email"
      />
      <select name="role" defaultValue="member" className="form-control">
        <option value="member">عضو فريق</option>
        <option value="admin">مدير</option>
      </select>

      {state.notice ? (
        <div className="flex items-center gap-2 rounded-xl bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800">
          <CheckCircle2 size={15} />
          {state.notice}
        </div>
      ) : null}
      {state.error ? (
        <div className="rounded-xl bg-red-50 px-3 py-2 text-xs font-bold text-red-700">
          {state.error}
        </div>
      ) : null}

      <Button className="w-full" disabled={pending}>
        {pending ? <LoaderCircle size={16} className="animate-spin" /> : null}
        {pending ? "جاري إرسال الدعوة..." : "إرسال الدعوة"}
      </Button>
    </form>
  );
}
