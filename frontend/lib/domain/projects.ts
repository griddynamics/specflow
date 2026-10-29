// FE-DATA-01 (TECH-SPECS §2.4).
import type { Unsubscribe } from "./common";

export type ProjectStatus = "empty" | "processing" | "attention" | "ready" | "unknown";
export interface Project {
  id: string;
  name: string;
  createdAt: string;
  status: ProjectStatus; // production list: "unknown" until Mission Control reads it (§3.4)
  description?: string; // switcher tooltip (MOCKS §1)
}

export interface ProjectsPort {
  list(): Promise<Project[]>; // newest first in both adapters
  get(projectId: string): Promise<Project>; // rejects not_found
  subscribe(listener: () => void): Unsubscribe; // list or a status changed
}

export const PROJECT_NAME_MAX = 200; // backend/src/api/schemas.py:12, 32-38

export function validateProjectName(raw: string): { ok: true; name: string } | { ok: false; reason: "empty" | "too_long" } {
  const name = raw.trim();
  if (name.length === 0) return { ok: false, reason: "empty" };
  if (name.length > PROJECT_NAME_MAX) return { ok: false, reason: "too_long" };
  return { ok: true, name };
}
