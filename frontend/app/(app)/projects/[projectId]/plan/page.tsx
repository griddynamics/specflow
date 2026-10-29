// FE-PLAN-01, FE-PLAN-11, FE-BP-01, FE-BP-03 (TECH-SPECS §1.2, §2.15): production tree. Thin binding, the (app) layout picks the production adapter.
import { Suspense } from "react";
import { PlanScreen } from "@/components/plan/PlanScreen";

export default async function Page({ params }: Readonly<{ params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  return (
    <Suspense>
      <PlanScreen projectId={projectId} />
    </Suspense>
  );
}
