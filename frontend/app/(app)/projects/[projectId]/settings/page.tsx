// docs/projects/ui.md#route-and-sidebar: Settings of one project, a server component like the Plan page. It reads the project's name once
// (`readProjectName`, the BFF's own call) and answers 404 for a project the caller cannot see; the screen is a client component.
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { SettingsScreen } from "@/components/settings/SettingsScreen";
import { readProjectName } from "@/lib/server-project";

export default async function Page({ params }: Readonly<{ params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  const project = await readProjectName(projectId);
  if (project === "not_found") notFound();
  return (
    <Suspense>
      <SettingsScreen projectId={projectId} projectName={project?.name ?? null} />
    </Suspense>
  );
}
