// AC1, AC8: the Integrations page of a project. A thin binding; the OAuth returns land here with `?github=` or `?atlassian=`. It reads the
// MCP address the deployment advertises (the Guides' values) for the MCP card.
import { Suspense } from "react";
import { IntegrationsPage } from "@/components/v2/integrations/IntegrationsPage";
import { guideConfig } from "@/lib/guides/guide-config";

export default async function Page({ params }: Readonly<{ params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  return (
    <Suspense>
      <IntegrationsPage projectId={projectId} mcp={guideConfig()} />
    </Suspense>
  );
}
