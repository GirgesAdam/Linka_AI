import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Linka | إدارة العيادة",
  description: "إدارة المواعيد والعملاء والمتابعات والتواصل من مكان واحد.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ar" dir="rtl"><body>{children}</body></html>;
}
