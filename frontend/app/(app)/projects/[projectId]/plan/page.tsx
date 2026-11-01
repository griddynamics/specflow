// FE-PLAN-01, FE-PLAN-11, FE-BP-01, FE-BP-03 (TECH-SPECS §1.2, §2.15): production tree. Thin binding, the (app) layout picks the production adapter.
// MCP-M10 (E11): the page is a server component. It reads the project's name once (`readProjectName`, the BFF's own call) and the guide
// config (`guideConfig`, server-only), and passes plain props down for Plan › Develop; no client module imports `lib/config.ts`.
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { PlanScreen } from "@/components/plan/PlanScreen";
import { guideConfig } from "@/lib/guides/guide-config";
import { readProjectName } from "@/lib/server-project";

export default async function Page({ params }: Readonly<{ params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  const project = await readProjectName(projectId);
  if (project === "not_found") notFound();
  const { mcpUrl, serverName } = guideConfig();
  return (
    <Suspense>
      <PlanScreen projectId={projectId} develop={{ projectName: project?.name ?? null, mcpUrl, serverName }} />
    </Suspense>
  );
}
