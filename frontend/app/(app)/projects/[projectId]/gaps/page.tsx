// FR-GWEB-04, FE-BP-01, FE-BP-03 (TECH-SPECS §1.2): production tree. Thin binding of the Gaps screen; the (app) layout picks the production adapter.
import { Suspense } from "react";
import { GapsScreen } from "@/components/gaps/GapsScreen";

export default async function Page({ params }: Readonly<{ params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  return (
    <Suspense>
      <GapsScreen projectId={projectId} />
    </Suspense>
  );
}
