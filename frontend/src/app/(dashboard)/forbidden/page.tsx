import { PermissionLink as Link } from "@/components/permission-link";

export default function ForbiddenPage() {
  return (
    <section className="mx-auto max-w-xl rounded-2xl border border-slate-200 bg-white p-8 text-center">
      <h1 className="text-xl font-bold text-slate-900">ليس لديك صلاحية للوصول إلى هذه الصفحة</h1>
      <p className="mt-3 text-sm text-slate-600">يمكن لمدير العيادة تعديل صلاحيات الصفحات الخاصة بحسابك.</p>
      <Link href="/dashboard" className="mt-5 inline-flex rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white">
        العودة إلى الرئيسية
      </Link>
    </section>
  );
}
