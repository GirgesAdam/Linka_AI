import { tiaRawRequest } from "@/lib/tia/api";

export async function GET() {
  const response = await tiaRawRequest("/clinic/history/template");
  const content = await response.arrayBuffer();
  return new Response(content, {
    status: 200,
    headers: {
      "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "Content-Disposition": 'attachment; filename="Linka_Import_Template_v1.xlsx"',
    },
  });
}
