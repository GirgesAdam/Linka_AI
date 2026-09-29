"use client";

import { useActionState, useState } from "react";
import { Check, CheckCircle2, Copy, ExternalLink, KeyRound, Link2, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  connectWhatsappDirectAction,
  finishWhatsappDirectSetupAction,
} from "./actions";
import type { WhatsAppSetupActionState } from "./actions";

export type WhatsAppSetupState = {
  connected: boolean;
  connection_status: "active" | "paused" | "disconnected" | null;
  display_name: string | null;
  display_phone_number: string | null;
  verified_name: string | null;
  provider_health_state: string | null;
  provider_error_code: string | null;
  provider_error: string | null;
  direct_setup_available: boolean;
  provider_credentials_ready: boolean;
  transport_ready: boolean;
  templates_ready: boolean;
  ready_for_automations: boolean;
  meta_app_id: string | null;
  webhook_callback_url: string | null;
  webhook_verify_token: string | null;
  webhook_verified: boolean;
  templates: Array<{
    rule_key: string;
    label: string;
    name: string;
    language: string;
    category: string;
    status: string;
    error: string | null;
  }>;
  admin_action:
    | "connect_meta_direct"
    | "configure_webhook"
    | "resolve_meta_restriction"
    | "wait_for_template_review"
    | "none";
  admin_message: string | null;
  system_message: string | null;
};

const APPS_URL = "https://developers.facebook.com/apps/";
const SYSTEM_USERS_URL = "https://business.facebook.com/settings/system-users";
const initialSetupActionState: WhatsAppSetupActionState = { ok: false, message: null };

function DirectLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 text-xs font-bold !text-teal-700 underline underline-offset-2"
    >
      {children} <ExternalLink size={12} />
    </a>
  );
}

function CopyValue({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex gap-2">
      <Input value={value} readOnly aria-label={label} className="font-mono text-xs" dir="ltr" />
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-label={`نسخ ${label}`}
        onClick={async () => {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        }}
      >
        <Copy size={14} /> {copied ? "تم" : "نسخ"}
      </Button>
    </div>
  );
}

function SetupProgress({ state }: { state: WhatsAppSetupState }) {
  const steps = [
    { label: "بيانات الربط", done: state.provider_credentials_ready },
    { label: "استقبال الرسائل", done: state.webhook_verified },
    { label: "جاهز للتشغيل", done: state.ready_for_automations },
  ];
  return (
    <ol className="grid gap-2 sm:grid-cols-3" aria-label="مراحل ربط واتساب">
      {steps.map((step, index) => (
        <li key={step.label} className={`flex items-center gap-2 rounded-xl border px-3 py-2 text-xs font-bold ${step.done ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-slate-200 bg-white text-slate-600"}`}>
          <span className={`grid size-6 shrink-0 place-items-center rounded-full ${step.done ? "bg-emerald-600 text-white" : "bg-slate-100 text-slate-600"}`}>
            {step.done ? <Check size={13} /> : index + 1}
          </span>
          {step.label}
        </li>
      ))}
    </ol>
  );
}

function TemplateStatusList({ templates }: { templates: WhatsAppSetupState["templates"] }) {
  const statusLabel = (status: string) => {
    const normalized = status.toLowerCase();
    if (normalized === "approved") return "معتمد";
    if (normalized === "pending") return "قيد مراجعة Meta";
    if (normalized === "rejected") return "يحتاج تعديل";
    if (normalized === "error") return "سيعاد التجهيز تلقائيًا";
    if (normalized === "disabled") return "متوقف في Meta";
    return "قيد التجهيز";
  };

  if (!templates.length) return null;

  return (
    <details className="rounded-2xl border border-slate-200 bg-white p-4">
      <summary className="cursor-pointer text-sm font-black text-slate-900">حالة قوالب الرسائل</summary>
      <p className="mt-2 text-xs leading-5 text-[var(--muted)]">
        Linka تنشئ القوالب المطلوبة وتستخدم القوالب المعتمدة فقط. لا تحتاج لإدارة أسماء تقنية من هنا.
      </p>
      <div className="mt-3 space-y-2">
        {templates.map((template) => (
          <div key={template.name} className="flex items-center justify-between gap-3 rounded-xl bg-slate-50 px-3 py-2">
            <div className="min-w-0 text-sm font-bold text-slate-900">{template.label}</div>
            <div className="shrink-0 text-xs font-bold text-slate-600">{statusLabel(template.status)}</div>
          </div>
        ))}
      </div>
    </details>
  );
}

export function WhatsAppDirectOnboarding({ state }: { state: WhatsAppSetupState }) {
  const [appId, setAppId] = useState(state.meta_app_id || "");
  const [appSecret, setAppSecret] = useState("");
  const [wabaId, setWabaId] = useState("");
  const [phoneNumberId, setPhoneNumberId] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [connectState, connectAction, connectPending] = useActionState(connectWhatsappDirectAction, initialSetupActionState);
  const [finishState, finishAction, finishPending] = useActionState(finishWhatsappDirectSetupAction, initialSetupActionState);

  const cleanAppId = appId.trim();
  const appDashboard = cleanAppId ? `${APPS_URL}${cleanAppId}/` : APPS_URL;
  const appSecretUrl = cleanAppId ? `${APPS_URL}${cleanAppId}/settings/basic/` : APPS_URL;
  const needsCredentials = !state.connected || state.admin_action === "connect_meta_direct" || !state.provider_credentials_ready;
  const needsWebhook = state.connected && !state.webhook_verified;

  return (
    <div className="space-y-5">
      <SetupProgress state={state} />

      {state.ready_for_automations ? (
        <>
          <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5">
            <div className="flex items-center gap-2 font-black text-emerald-950">
              <CheckCircle2 size={20} /> واتساب جاهز
            </div>
            <p className="mt-2 text-sm leading-6 text-emerald-900">
              {state.verified_name || state.display_phone_number || "رقم العيادة"} متصل ويستقبل الرسائل، والرسائل التلقائية الجاهزة يمكن تشغيلها من صفحتها.
            </p>
          </div>
          <TemplateStatusList templates={state.templates || []} />
        </>
      ) : (
        <>
          {needsCredentials && (
            <form action={connectAction} className="space-y-5 rounded-2xl border border-teal-200 bg-teal-50/40 p-5">
              <div>
                <div className="flex items-center gap-2 font-black text-slate-950">
                  <KeyRound size={18} /> 1. أدخل بيانات الربط من Meta
                </div>
                <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                  جهّز الرقم في WhatsApp Cloud API أولًا، ثم انسخ القيم التالية. Linka تتحقق منها قبل الحفظ، والبيانات السرية لا تظهر مرة أخرى بعد نجاح الربط.
                </p>
                <details className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-950">
                  <summary className="cursor-pointer font-bold">لو الرقم مستخدم حاليًا في WhatsApp Business App</summary>
                  <p className="mt-2">
                    الربط اليدوي الحالي لا يستخدم Coexistence. لو ستستخدم نفس الرقم على Cloud API، أكمل نقل وتجهيز الرقم داخل Meta أولًا وجهّز فريق الاستقبال لاستخدام Inbox داخل Linka للرد اليدوي.
                  </p>
                </details>
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="space-y-2 text-sm font-bold text-slate-900">
                  <span>معرّف التطبيق <span className="font-normal text-slate-500">(App ID)</span></span>
                  <Input name="app_id" value={appId} onChange={(event) => setAppId(event.target.value)} inputMode="numeric" dir="ltr" placeholder="1234567890123456" required />
                  <DirectLink href={APPS_URL}>أين أجده؟ افتح My Apps</DirectLink>
                </label>

                <label className="space-y-2 text-sm font-bold text-slate-900">
                  <span>مفتاح التطبيق السري <span className="font-normal text-slate-500">(App Secret)</span></span>
                  <Input name="app_secret" type="password" dir="ltr" autoComplete="off" value={appSecret} onChange={(event) => setAppSecret(event.target.value)} required />
                  <DirectLink href={appSecretUrl}>أين أجده؟ App Settings → Basic</DirectLink>
                </label>

                <label className="space-y-2 text-sm font-bold text-slate-900">
                  <span>معرّف حساب واتساب <span className="font-normal text-slate-500">(WABA ID)</span></span>
                  <Input name="waba_id" inputMode="numeric" dir="ltr" value={wabaId} onChange={(event) => setWabaId(event.target.value)} required />
                  <DirectLink href={appDashboard}>أين أجده؟ WhatsApp → API Setup</DirectLink>
                </label>

                <label className="space-y-2 text-sm font-bold text-slate-900">
                  <span>معرّف رقم الهاتف <span className="font-normal text-slate-500">(Phone Number ID)</span></span>
                  <Input name="phone_number_id" inputMode="numeric" dir="ltr" value={phoneNumberId} onChange={(event) => setPhoneNumberId(event.target.value)} required />
                  <DirectLink href={appDashboard}>أين أجده؟ WhatsApp → API Setup</DirectLink>
                </label>
              </div>

              <label className="block space-y-2 text-sm font-bold text-slate-900">
                <span>رمز الوصول الدائم <span className="font-normal text-slate-500">(System User Access Token)</span></span>
                <Input name="access_token" type="password" dir="ltr" autoComplete="off" value={accessToken} onChange={(event) => setAccessToken(event.target.value)} required />
                <div><DirectLink href={SYSTEM_USERS_URL}>فتح System Users</DirectLink></div>
              </label>

              <details className="rounded-xl border border-slate-200 bg-white p-3 text-xs leading-5 text-slate-600">
                <summary className="cursor-pointer font-bold text-slate-900">طريقة تجهيز رمز الوصول</summary>
                <p className="mt-2">
                  أنشئ System User بصلاحية Admin، اربطه بالـApp وWhatsApp Account، ثم أنشئ Token بصلاحيتي whatsapp_business_management وwhatsapp_business_messaging.
                </p>
              </details>

              {connectState.message && (
                <p role="status" aria-live="polite" className={`rounded-xl p-3 text-sm leading-6 ${connectState.ok ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"}`}>
                  {connectState.message}
                </p>
              )}

              <div className="flex flex-wrap items-center gap-3">
                <Button type="submit" disabled={connectPending || !state.direct_setup_available}>
                  {connectPending ? "جارٍ التحقق..." : "تحقق واربط"}
                </Button>
                {!state.direct_setup_available && <span className="text-xs text-rose-700">إعداد الربط غير متاح مؤقتًا. حاول مرة أخرى لاحقًا.</span>}
              </div>
            </form>
          )}

          {needsWebhook && state.webhook_callback_url && state.webhook_verify_token && (
            <form action={finishAction} className="space-y-4 rounded-2xl border border-sky-200 bg-sky-50/50 p-5">
              <div>
                <div className="flex items-center gap-2 font-black text-slate-950">
                  <Link2 size={18} /> 2. فعّل استقبال الرسائل
                </div>
                <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                  افتح WhatsApp → Configuration في نفس Meta App، وانسخ القيمتين التاليتين ثم اختر Verify and Save. بعد ذلك فعّل الاشتراك في messages.
                </p>
              </div>

              <div className="space-y-2">
                <div className="text-xs font-bold text-slate-700">رابط الاستقبال (Callback URL)</div>
                <CopyValue value={state.webhook_callback_url} label="Callback URL" />
              </div>
              <div className="space-y-2">
                <div className="text-xs font-bold text-slate-700">رمز التحقق (Verify Token)</div>
                <CopyValue value={state.webhook_verify_token} label="Verify Token" />
              </div>

              <DirectLink href={appDashboard}>فتح WhatsApp → Configuration</DirectLink>

              {finishState.message && (
                <p role="status" aria-live="polite" className={`rounded-xl p-3 text-sm ${finishState.ok ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"}`}>
                  {finishState.message}
                </p>
              )}

              <Button type="submit" disabled={finishPending}>
                {finishPending ? "جارٍ التحقق..." : "تم في Meta — تحقق وكمل"}
              </Button>
            </form>
          )}

          {state.connected && state.webhook_verified && !state.ready_for_automations && (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5">
              <div className="flex items-center gap-2 font-black text-amber-950">
                <ShieldCheck size={18} /> 3. فحص الجاهزية
              </div>
              <p className="mt-2 text-sm leading-6 text-amber-900">
                {state.system_message || state.admin_message || "استقبال الرسائل اتأكد. Linka تفحص الرقم والقوالب ومسار الإرسال قبل السماح بالتشغيل."}
              </p>
            </div>
          )}

          {state.connected && <TemplateStatusList templates={state.templates || []} />}
        </>
      )}
    </div>
  );
}
