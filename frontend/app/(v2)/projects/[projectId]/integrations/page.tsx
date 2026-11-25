// AC1, AC8: the Integrations page of a project. A thin binding; the OAuth returns land here with `?github=` or `?atlassian=`.
import { Suspense } from "react";
import { IntegrationsPage } from "@/components/v2/integrations/IntegrationsPage";

export default async function Page({ params }: Readonly<{ params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  return (
    <Suspense>
      <IntegrationsPage projectId={projectId} />
    </Suspense>
  );
}
