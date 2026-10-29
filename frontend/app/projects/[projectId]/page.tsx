// FE-BP-03.AC7 (TECH-SPECS §1.7): the old hello project page. Upload now lives in Mission Control, so old links redirect there
// (307). The target route checks the session and the project itself.
import { redirect } from "next/navigation";
import { routesFor } from "@/lib/routes";

export default async function OldProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  redirect(routesFor("production").missionControl(projectId));
}
