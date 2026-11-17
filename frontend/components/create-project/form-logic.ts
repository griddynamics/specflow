// FE-CRT-01, FE-CRT-02, FE-CRT-06, FE-DEMO-08 (PLAN a.4), FR-PRJ-UI-09/10: the pure parts of the Create project form, so they run in node tests.
// The form has the name and the collaborators by email (sources are added on Mission Control).
import type { CreateProjectInput } from "@/lib/domain/create-project";
import { validateProjectName } from "@/lib/domain/projects";
import { COPY } from "@/lib/copy";

/** At most 50 people per project and the creator is one of them (docs/projects/api.md#create-with-collaborators). */
export const COLLABORATOR_CHIPS_MAX = 49;

export interface FormState {
  name: string;
  emails: string[]; // normalised (trimmed, lower-case), unique, in chip order
}

/** FE-DEMO-08.AC1: the defaults every opening starts from. */
export function initialForm(): FormState {
  return { name: "", emails: [] };
}

/** FE-CRT-01.AC2/AC3 (PA6): the message under the Name field, or null when the name is fine. */
export function nameError(raw: string): string | null {
  const v = validateProjectName(raw);
  if (v.ok) return null;
  return v.reason === "empty" ? COPY.nameEmpty : COPY.nameTooLong;
}

/** FE-CRT-06: the port input, or null while the name is not valid. `collaboratorEmails` is left out when no chip is set. */
export function buildInput(f: FormState): CreateProjectInput | null {
  const v = validateProjectName(f.name);
  if (!v.ok) return null;
  return f.emails.length > 0 ? { name: v.name, collaboratorEmails: f.emails } : { name: v.name };
}

/** The text under the Collaborators field for a typed text the picker refused. */
export function rejectText(reason: "invalid" | "duplicate" | "full"): string {
  if (reason === "invalid") return COPY.emailInvalid;
  if (reason === "duplicate") return COPY.alreadyCollaborator;
  return COPY.memberLimit;
}

const CHIP_TEXT: Record<string, string> = {
  invalid_email: COPY.emailInvalid,
  email_domain_not_allowed: COPY.emailDomainNotAllowed,
};

export interface CreateFailure {
  nameMsg: string | null;
  alert: boolean;
  /** Messages by chip index (`details.errors[].loc[2]`); empty when the failure is not about the emails. */
  chipMsgs: Record<number, string>;
}

/** The list index in `loc[2]`: the API sends every `loc` part as a string (`"1"`); a number is accepted too. */
function chipIndex(part: string | number | undefined): number | null {
  if (typeof part === "number") return Number.isInteger(part) && part >= 0 ? part : null;
  return typeof part === "string" && /^\d+$/.test(part) ? Number(part) : null;
}

/** The chip errors of a 422: entries on `body.collaborators[i]` with a rule we have a text for. */
function chipMessages(e: unknown): Record<number, string> {
  const errors = (e as { detail?: { errors?: { loc: (string | number)[]; type: string }[] } }).detail?.errors ?? [];
  const out: Record<number, string> = {};
  for (const x of errors) {
    const i = chipIndex(x.loc[2]);
    if (x.loc[1] === "collaborators" && i !== null) out[i] = CHIP_TEXT[x.type] ?? COPY.emailInvalid;
  }
  return out;
}

/** FE-CRT-01 (PA6), FR-PRJ-UI-10: what a failed `create` shows. Chip entries mark chips; another `validation` is a name message; anything else is the alert. */
export function createFailure(e: unknown, rawName: string): CreateFailure {
  const validation = typeof e === "object" && e !== null && (e as { code?: string }).code === "validation";
  if (!validation) return { nameMsg: null, alert: true, chipMsgs: {} };
  const chipMsgs = chipMessages(e);
  if (Object.keys(chipMsgs).length > 0) return { nameMsg: null, alert: false, chipMsgs };
  return { nameMsg: nameError(rawName) ?? "Enter a valid project name.", alert: false, chipMsgs };
}
