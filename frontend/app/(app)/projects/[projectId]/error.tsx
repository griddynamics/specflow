"use client";
// FE-BP-01, FE-BP-03 (TECH-SPECS §1.2): production tree. The error boundary of a project's screens: a screen that throws (an unknown or unreadable project) falls back to a retry state inside the shell.
import { ProjectErrorState } from "@/components/shell/ProjectErrorState";

export default function ProjectSegmentError({ retry }: Readonly<{ retry: () => void }>) {
  return <ProjectErrorState onRetry={retry} />;
}
