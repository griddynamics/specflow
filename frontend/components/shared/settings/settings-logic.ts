// docs/projects/ui.md#general-tab, #errors-shown: the rules of the General tab, free of React. Which error goes where, the name rule,
// the rows of the list, and the actions that run through the ports.
import { isEmailShape } from "@/components/shared/ui/app-logic";
import { COPY } from "@/lib/copy";
import type { AddedPerson, ClaimsOutcome, Invite, Member, MembersPort, MembersState } from "@/lib/domain/members";
import { PROJECT_NAME_MAX, type ProjectsPort } from "@/lib/domain/projects";
import { isDataError } from "@/lib/domain/errors";
import { SETTINGS_COPY } from "./settings-copy";

// ---- the name block --------------------------------------------------------------------------------------------------------------

export type NameCheck = { kind: "same" } | { kind: "ok"; name: string } | { kind: "empty" } | { kind: "too_long" };

/** The backend name rule: trimmed, 1-200 code points. `same` = the trimmed text is the saved name (Save stays off, no error). */
export function checkName(raw: string, saved: string): NameCheck {
  const name = raw.trim();
  if (name === saved) return { kind: "same" };
  const length = Array.from(name).length;
  if (length === 0) return { kind: "empty" };
  if (length > PROJECT_NAME_MAX) return { kind: "too_long" };
  return { kind: "ok", name };
}

export function nameErrorText(c: NameCheck): string | undefined {
  if (c.kind === "empty") return COPY.nameEmpty;
  if (c.kind === "too_long") return COPY.nameTooLong;
  return undefined;
}

// ---- the add row -----------------------------------------------------------------------------------------------------------------

/** What a key does in the add row. `count` = suggestions on hand (maybe none yet), `active` = the highlighted one. */
export type AddKey =
  { kind: "move"; index: number; open: boolean } | { kind: "pick"; index: number } | { kind: "add" } | { kind: "close" } | null;

export function addRowKey(key: string, s: { active: number; count: number; showList: boolean; busy: boolean }): AddKey {
  const last = Math.max(s.count - 1, 0);
  const active = Math.min(Math.max(s.active, 0), last);
  if (key === "ArrowDown") return { kind: "move", index: Math.min(active + 1, last), open: true }; // no items yet: stays 0, never -1
  if (key === "ArrowUp") return { kind: "move", index: Math.max(active - 1, 0), open: false };
  if (key === "Enter") {
    if (s.showList && s.count > 0) return { kind: "pick", index: active };
    return s.busy ? null : { kind: "add" };
  }
  if (key === "Escape" && s.showList) return { kind: "close" };
  return null;
}

/** What a failure means for the screen. `gone`: the project is no longer ours, go home. */
export type Failure = { kind: "field"; text: string } | { kind: "toast"; text: string; reload: boolean } | { kind: "gone" };

const FIELD_TEXT: Record<string, string> = {
  invalid_email: COPY.emailInvalid,
  email_domain_not_allowed: COPY.emailDomainNotAllowed,
  already_member: COPY.alreadyCollaborator,
  already_invited: COPY.alreadyInvited,
  member_limit: COPY.memberLimit,
};
const STALE = new Set(["member_not_found", "invite_not_found", "cannot_remove_owner"]);

const apiCodeOf = (e: unknown): string | undefined => (isDataError(e) ? e.detail?.apiCode : undefined);
export const isGone = (e: unknown): boolean => apiCodeOf(e) === "project_not_found";

/** A failure of "Add": the field says it, except when the project is gone. */
export function addFailure(e: unknown): Failure {
  const code = apiCodeOf(e);
  if (isGone(e)) return { kind: "gone" };
  const text = code === undefined ? undefined : FIELD_TEXT[code];
  if (text !== undefined) return { kind: "field", text };
  return { kind: "toast", text: COPY.saveFailed, reload: false };
}

/** A failure of remove, cancel or leave: a stale list reloads with a toast, anything else is "could not save". */
export function changeFailure(e: unknown): Failure {
  const code = apiCodeOf(e);
  if (code !== undefined && STALE.has(code)) return { kind: "toast", text: COPY.listChanged, reload: true };
  if (isGone(e)) return { kind: "gone" };
  return { kind: "toast", text: COPY.saveFailed, reload: false };
}

/** A failure of "Save" on the name: the rule messages belong under the field, the rest is a toast. */
export function renameFailure(e: unknown): Failure {
  if (isDataError(e) && e.code === "validation") {
    const tooLong = e.detail?.errors?.some((x) => x.type.includes("too_long") || x.type.includes("max")) ?? false;
    return { kind: "field", text: tooLong ? COPY.nameTooLong : COPY.nameEmpty };
  }
  if (isGone(e)) return { kind: "gone" };
  return { kind: "toast", text: COPY.saveFailed, reload: false };
}

// ---- the list --------------------------------------------------------------------------------------------------------------------

export type RowAction = { kind: "remove"; userId: string } | { kind: "cancel"; inviteId: string };

export interface Row {
  key: string;
  type: "member" | "invite";
  /** The first line: the name, or the email when there is no name (an invite is its email). */
  title: string;
  /** The second line. */
  subtitle: string;
  badge: "owner" | "invited" | null;
  /** The x of the row (owner view only): what it would do. null = no x. */
  action: RowAction | null;
}

const memberTitle = (m: Member): string => (m.name !== null && m.name !== "" ? m.name : m.email);

/** The rows in API order (the owner, members by join date, then invites by creation). `isOwner`: the caller may remove (never the owner's row). */
export function rowsOf(state: MembersState, isOwner: boolean): Row[] {
  const members = state.members.map((m): Row => ({
    key: `m:${m.userId}`,
    type: "member",
    title: memberTitle(m),
    subtitle: memberTitle(m) === m.email ? "" : m.email,
    badge: m.role === "owner" ? "owner" : null,
    action: isOwner && m.role !== "owner" ? { kind: "remove", userId: m.userId } : null,
  }));
  const invites = state.invites.map((i: Invite): Row => ({
    key: `i:${i.id}`,
    type: "invite",
    title: i.email,
    subtitle: COPY.notSignedInYet,
    badge: "invited",
    action: isOwner ? { kind: "cancel", inviteId: i.id } : null,
  }));
  return [...members, ...invites];
}

/** "Mark Lee" or the invite's email: what a confirm dialog names. */
export const rowLabel = (r: Row): string => r.title;

// ---- the confirm dialogs ---------------------------------------------------------------------------------------------------------

export type Confirm =
  { kind: "remove"; userId: string; label: string } | { kind: "cancel"; inviteId: string; label: string } | { kind: "leave" };

export interface ConfirmText {
  title: string;
  body: string;
  confirm: string;
  dismiss: string;
}

export function confirmText(c: Confirm): ConfirmText {
  if (c.kind === "remove") {
    return {
      title: SETTINGS_COPY.removeTitle,
      body: `${c.label} ${SETTINGS_COPY.removeBody}`,
      confirm: SETTINGS_COPY.remove,
      dismiss: SETTINGS_COPY.keep,
    };
  }
  if (c.kind === "cancel") {
    return {
      title: SETTINGS_COPY.cancelTitle,
      body: `${c.label} ${SETTINGS_COPY.cancelBody}`,
      confirm: SETTINGS_COPY.cancelInvite,
      dismiss: SETTINGS_COPY.keep,
    };
  }
  return { title: SETTINGS_COPY.leaveTitle, body: SETTINGS_COPY.leaveBody, confirm: COPY.leaveProject, dismiss: SETTINGS_COPY.keep };
}

export function confirmOf(r: Row): Confirm | null {
  if (r.action === null) return null;
  if (r.action.kind === "remove") return { kind: "remove", userId: r.action.userId, label: rowLabel(r) };
  return { kind: "cancel", inviteId: r.action.inviteId, label: rowLabel(r) };
}

// ---- running an action through the ports -----------------------------------------------------------------------------------------

export interface Effects {
  toast: (t: { title: string; tone?: "success" | "info" | "danger" }) => void;
  /** The list is stale: read it again. */
  reload: () => void;
  /** The caller left: the switcher reloads and the app opens `/`. */
  left: () => void;
  /** The project is gone for the caller (removed meanwhile): the app opens `/`. */
  gone: () => void;
}

const claimsToast = (o: ClaimsOutcome, fx: Effects): boolean => {
  if (!o.claimsReleaseFailed) return false;
  fx.toast({ title: COPY.claimsNotFreed, tone: "info" });
  return true;
};

/** The port call of a confirmed action, with its success toast; `leave` ends in `left`, the others reload the list. */
async function callConfirmed(c: Confirm, projectId: string, members: MembersPort, fx: Effects): Promise<void> {
  if (c.kind === "remove") {
    const o = await members.remove(projectId, c.userId);
    if (!claimsToast(o, fx)) fx.toast({ title: SETTINGS_COPY.toastRemoved, tone: "success" });
    fx.reload();
  } else if (c.kind === "cancel") {
    await members.cancelInvite(projectId, c.inviteId);
    fx.toast({ title: SETTINGS_COPY.toastInviteCancelled, tone: "success" });
    fx.reload();
  } else {
    const o = await members.leave(projectId);
    if (!claimsToast(o, fx)) fx.toast({ title: SETTINGS_COPY.toastLeft, tone: "success" });
    fx.left();
  }
}

/** Runs a confirmed remove, cancel or leave. Resolves true when it went through. A failure becomes a toast, a reload or `gone`. */
export async function runConfirm(c: Confirm, projectId: string, members: MembersPort, fx: Effects): Promise<boolean> {
  try {
    await callConfirmed(c, projectId, members, fx);
    return true;
  } catch (e: unknown) {
    const f = changeFailure(e);
    if (f.kind === "gone") fx.gone();
    else if (f.kind === "toast") {
      fx.toast({ title: f.text, tone: f.reload ? "info" : "danger" });
      if (f.reload) fx.reload();
    }
    return false;
  }
}

/** Runs "Add". Resolves the added person, or the text to show under the field. */
export async function runAdd(
  raw: string,
  projectId: string,
  members: MembersPort,
  fx: Effects,
): Promise<{ ok: true; added: AddedPerson } | { ok: false; error: string | null }> {
  const email = raw.trim();
  if (!isEmailShape(email)) return { ok: false, error: COPY.emailInvalid };
  try {
    const added = await members.add(projectId, email);
    fx.toast({ title: added.kind === "member" ? SETTINGS_COPY.toastAdded : SETTINGS_COPY.toastInvited, tone: "success" });
    fx.reload();
    return { ok: true, added };
  } catch (e: unknown) {
    const f = addFailure(e);
    if (f.kind === "field") return { ok: false, error: f.text };
    if (f.kind === "gone") fx.gone();
    else fx.toast({ title: f.text, tone: "danger" });
    return { ok: false, error: null };
  }
}

/** Runs "Save" on the name. Resolves the saved name, or the text to show under the field. */
export async function runRename(
  name: string,
  projectId: string,
  projects: ProjectsPort,
  fx: Effects,
): Promise<{ ok: true; name: string } | { ok: false; error: string | null }> {
  try {
    const renamed = await projects.rename(projectId, name); // the port notifies the switcher
    fx.toast({ title: SETTINGS_COPY.toastRenamed, tone: "success" });
    return { ok: true, name: renamed.name };
  } catch (e: unknown) {
    const f = renameFailure(e);
    if (f.kind === "field") return { ok: false, error: f.text };
    if (f.kind === "gone") fx.gone();
    else fx.toast({ title: f.text, tone: "danger" });
    return { ok: false, error: null };
  }
}
