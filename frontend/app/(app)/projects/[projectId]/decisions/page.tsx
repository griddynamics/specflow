// FE-BP-01, FE-BP-03 (TECH-SPECS §1.2): production tree (P17). Thin binding, the (app) layout picks the production adapter.
import { Suspense } from "react";
import { DecisionsScreen } from "@/components/decisions/DecisionsScreen";

export default async function Page({ params }: Readonly<{ params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  return (
    <Suspense>
      <DecisionsScreen projectId={projectId} />
    </Suspense>
  );
}
