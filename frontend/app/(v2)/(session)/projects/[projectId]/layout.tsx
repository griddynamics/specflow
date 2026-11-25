// AC1: the project gate of the V2 screens. It makes the call the BFF would make (`readProjectName`) and answers 404 for a project the
// caller cannot see; an unreadable answer (the API is down) still renders, and the screen shows its own failure.
import { notFound } from "next/navigation";
import type { ReactNode } from "react";
import { readProjectName } from "@/lib/server-project";

export default async function ProjectLayout({
  children,
  params,
}: Readonly<{ children: ReactNode; params: Promise<{ projectId: string }> }>) {
  const { projectId } = await params;
  if ((await readProjectName(projectId)) === "not_found") notFound();
  return children;
}
