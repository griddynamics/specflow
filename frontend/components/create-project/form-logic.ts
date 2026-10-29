// FE-CRT-01, FE-CRT-02, FE-CRT-06, FE-DEMO-08 (PLAN a.4): the pure parts of the Create project form, so they run in node tests.
// Owner decision 2026-10-15: the form has the name and the contributors only (sources are added on Mission Control).
import type { CreateProjectInput } from "@/lib/domain/create-project";
import { PROJECT_NAME_MAX, validateProjectName } from "@/lib/domain/projects";

export interface FormState {
  name: string;
  contributorIds: string[];
}

/** FE-DEMO-08.AC1: the defaults every opening starts from. */
export function initialForm(): FormState {
  return { name: "", contributorIds: [] };
}

/** FE-CRT-01.AC2/AC3 (PA6): the message under the Name field, or null when the name is fine. */
export function nameError(raw: string): string | null {
  const v = validateProjectName(raw);
  if (v.ok) return null;
  return v.reason === "empty" ? "Enter a project name." : `Use ${String(PROJECT_NAME_MAX)} characters or fewer.`;
}

/** FE-CRT-06: the port input, or null while the name is not valid. */
export function buildInput(f: FormState): CreateProjectInput | null {
  const v = validateProjectName(f.name);
  if (!v.ok) return null;
  return { name: v.name, contributorIds: f.contributorIds };
}

/** FE-CRT-01 (PA6): what a failed `create` shows. A `validation` error is a name message; anything else is the alert. */
export function createFailure(e: unknown, rawName: string): { nameMsg: string | null; alert: boolean } {
  const validation = typeof e === "object" && e !== null && (e as { code?: string }).code === "validation";
  return validation ? { nameMsg: nameError(rawName) ?? "Enter a valid project name.", alert: false } : { nameMsg: null, alert: true };
}
