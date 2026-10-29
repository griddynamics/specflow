// FE-BP-03.AC7 (TECH-SPECS §1.7, A10, O11, DECISIONS G01-13): the old hello document page. Real document contents have no screen in
// the new app, so the link redirects (307) to the project's Mission Control; summary checks read the API instead.
import { redirect } from "next/navigation";
import { routesFor } from "@/lib/routes";

export default async function OldDocumentPage({ params }: { params: Promise<{ projectId: string; documentId: string }> }) {
  const { projectId } = await params;
  redirect(routesFor("production").missionControl(projectId));
}
