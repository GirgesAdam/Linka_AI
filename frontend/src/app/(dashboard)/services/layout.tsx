import { requireSectionAccess } from "@/lib/tia/require-section-access";

export default async function SectionAccessLayout({ children }: { children: React.ReactNode }) {
  await requireSectionAccess("clinic_operations");
  return children;
}
