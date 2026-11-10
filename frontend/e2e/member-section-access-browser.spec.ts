import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import * as ts from "typescript";

const helperPath = resolve(__dirname, "../src/lib/section-access.ts");
const helperSource = readFileSync(helperPath, "utf8");
const helperJavaScript = ts.transpileModule(helperSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

test("browser navigation contract keeps defaults open and gates only the two sections", async ({ page }) => {
  await page.goto("about:blank");
  await page.setContent("<nav id='navigation'></nav>");
  await page.addScriptTag({
    content:
      "window.__sectionAccess = {}; (function(exports) { " +
      helperJavaScript +
      " })(window.__sectionAccess);",
  });

  const result = await page.evaluate(() => {
    const policy = (window as typeof window & {
      __sectionAccess: {
        canAccessRoute: (workspace: unknown, href: string) => boolean;
      };
    }).__sectionAccess;
    const routes = [
      "/dashboard",
      "/inbox",
      "/appointments",
      "/patients",
      "/tasks",
      "/doctors",
      "/services",
      "/inventory",
      "/analytics",
      "/finance",
      "/automations",
      "/setup",
      "/team",
    ];
    const states = {
      closed: { role: "member", section_access: { clinic_operations: false, administration: false } },
      clinic: { role: "member", section_access: { clinic_operations: true, administration: false } },
      administration: { role: "member", section_access: { clinic_operations: false, administration: true } },
      admin: { role: "admin", section_access: { clinic_operations: false, administration: false } },
    };
    const navigation = document.querySelector("#navigation")!;
    const visibleByState: Record<string, string[]> = {};
    for (const [stateName, workspace] of Object.entries(states)) {
      const visible = routes.filter((route) => policy.canAccessRoute(workspace, route));
      visibleByState[stateName] = visible;
      navigation.replaceChildren(
        ...visible.map((href) => {
          const link = document.createElement("a");
          link.href = href;
          link.textContent = href;
          return link;
        }),
      );
      if (stateName === "closed") {
        visibleByState[stateName] = Array.from(navigation.querySelectorAll("a")).map((link) => link.getAttribute("href")!);
      }
    }
    return visibleByState;
  });

  expect(result.closed).toEqual([
    "/dashboard",
    "/inbox",
    "/appointments",
    "/patients",
    "/tasks",
  ]);
  expect(result.clinic).toContain("/doctors");
  expect(result.clinic).toContain("/inventory");
  expect(result.clinic).not.toContain("/finance");
  expect(result.administration).toContain("/analytics");
  expect(result.administration).toContain("/setup");
  expect(result.administration).not.toContain("/services");
  expect(result.administration).not.toContain("/team");
  expect(result.admin).toEqual([
    "/dashboard",
    "/inbox",
    "/appointments",
    "/patients",
    "/tasks",
    "/doctors",
    "/services",
    "/inventory",
    "/analytics",
    "/finance",
    "/automations",
    "/setup",
    "/team",
  ]);
});
