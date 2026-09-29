import { tiaRawRequest } from "@/lib/tia/api";

export async function GET() {
  const response = await tiaRawRequest("/clinic/setup-v2/template");
  const content = await response.arrayBuffer();
  return new Response(content, {
    status: 200,
    headers: {
      "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "Content-Disposition": 'attachment; filename="Linka_Clinic_Setup_Template_v1.xlsx"',
    },
  });
}
