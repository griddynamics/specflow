// docs/jira-sync/ui.md#jira-tab, #errors-shown, oauth.md#jira-tab: the rules of the Jira tab, free of React. The button phase, which text
// goes where after a failed check or save, the texts of the reason codes and the status word, and in the `oauth` mode the caller's
// Atlassian connection row, the acting account line and the `?atlassian=` toast.
import { COPY } from "@/lib/copy";
import { isDataError, type DataError } from "@/lib/domain/errors";
import type {
  AtlassianConnection,
  JiraAccess,
  JiraActing,
  JiraAccessField,
  JiraSettings,
  JiraSyncProgress,
  JiraSyncState,
  JiraSyncStep,
  JiraTarget,
} from "@/lib/domain/jira";
import { countText, SETTINGS_COPY } from "./settings-copy";

/** The typed keys as the API reads them: trimmed, upper-cased, a blank Epic is none. */
export function targetOf(project: string, epic: string): JiraTarget {
  const key = epic.trim().toUpperCase();
  return { jiraProject: project.trim().toUpperCase(), epicKey: key === "" ? null : key };
}

/** What the last check or save left on the form: the answer of a passed check, or the texts of a failure. */
export type Outcome = { kind: "ok"; projectName: string; epicSummary: string | null } | { kind: "problem"; texts: Problem };
export interface Problem {
  project?: string;
  epic?: string;
  alert?: string;
}

export type Phase = "check" | "checking" | "save" | "saving";

/** "Check access" until a check of the current values passed, then "Save"; `busy` is the call in flight. */
export function phaseOf(outcome: Outcome | null, busy: "check" | "save" | null): Phase {
  if (busy === "check") return "checking";
  if (busy === "save") return "saving";
  return outcome?.kind === "ok" ? "save" : "check";
}

export const PHASE_LABEL: Record<Phase, string> = {
  check: SETTINGS_COPY.checkAccess,
  checking: SETTINGS_COPY.checking,
  save: COPY.settingsSave,
  saving: SETTINGS_COPY.saving,
};

const REASON_TEXT: Record<string, string> = {
  jira_auth_failed: COPY.jiraAuthFailed,
  jira_forbidden: COPY.jiraForbidden,
  jira_issue_types_missing: COPY.jiraIssueTypesMissing,
  jira_epic_invalid: COPY.jiraEpicInvalid,
  jira_unreachable: COPY.jiraUnreachable,
  jira_unexpected: COPY.jiraUnreachable,
  no_plan: COPY.jiraNoPlan,
  plan_applying: COPY.jiraPlanApplying,
  jira_not_configured: COPY.jiraNotConfigured,
  interrupted: COPY.jiraInterrupted,
  target_changed: COPY.jiraTargetChanged,
  jira_not_connected: COPY.jiraNotConnected,
  jira_reconnect_required: COPY.jiraReconnectRequired,
};

/** A pass that ended on the target's Atlassian connection names the owner's part, not the caller's (docs/jira-sync/oauth.md). */
const SYNC_TEXT: Record<string, string> = {
  jira_not_connected: COPY.jiraSyncNotConnected,
  jira_reconnect_required: COPY.jiraSyncReconnect,
};

/**
 * The text of an access-check reason or a sync `error_code`; `field` tells the two `jira_not_found` apart. `oauth`: the server's Jira
 * mode is `oauth`, where `jira_auth_failed` is the user's Atlassian connection, not the deployment's token.
 */
export function reasonText(code: string, field: JiraAccessField | null = null, oauth = false): string {
  if (code === "jira_not_found") return field === "epic_key" ? COPY.jiraEpicNotFound : COPY.jiraProjectNotFound;
  if (code === "jira_auth_failed" && oauth) return COPY.jiraAuthFailedOAuth;
  return Object.hasOwn(REASON_TEXT, code) ? REASON_TEXT[code] : COPY.jiraUnexpectedError;
}

/** The text of a sync `error_code` on the status line; in the `oauth` mode `jira_auth_failed` names the target's owner. */
export function syncErrorText(code: string, oauth = false): string {
  if (code === "jira_auth_failed" && oauth) return COPY.jiraSyncAuthFailedOAuth;
  return Object.hasOwn(SYNC_TEXT, code) ? SYNC_TEXT[code] : reasonText(code);
}

/** A check answer as the form's outcome: the reason sits under the field it names, or is the alert line. */
export function outcomeOf(access: JiraAccess, oauth = false): Outcome {
  if (access.result === "ok") return { kind: "ok", projectName: access.projectName, epicSummary: access.epicSummary };
  const text = reasonText(access.reason, access.field, oauth);
  if (access.field === "jira_project") return { kind: "problem", texts: { project: text } };
  return { kind: "problem", texts: access.field === "epic_key" ? { epic: text } : { alert: text } };
}

/**
 * What a failed call means for the screen: `gone` (go home), `unavailable` (the notice), fields under the inputs, or a toast.
 * `disconnected` (a PUT answered 409 `jira_not_connected`: the connection expired or went meanwhile) is the alert line, and the tab reads
 * the connection again.
 */
export type Failure =
  | { kind: "gone" }
  | { kind: "unavailable" }
  | { kind: "fields"; texts: Problem }
  | { kind: "disconnected"; text: string }
  | { kind: "toast"; text: string };

/** The inputs a 422 names (`loc[1]`), each with the key text; empty when it names none. */
function keyProblems(e: DataError): Problem {
  const texts: Problem = {};
  for (const entry of e.detail?.errors ?? []) {
    if (entry.loc[1] === "jira_project") texts.project = COPY.jiraKeyInvalid;
    if (entry.loc[1] === "epic_key") texts.epic = COPY.jiraKeyInvalid;
  }
  return texts;
}

export function failureOf(e: unknown): Failure {
  const code = isDataError(e) ? e.detail?.apiCode : undefined;
  if (code === "project_not_found") return { kind: "gone" };
  if (code === "jira_unavailable") return { kind: "unavailable" };
  if (code === "jira_not_connected") return { kind: "disconnected", text: COPY.jiraNotConnected };
  const texts = isDataError(e) && code === "validation_failed" ? keyProblems(e) : {};
  return Object.keys(texts).length > 0 ? { kind: "fields", texts } : { kind: "toast", text: COPY.saveFailed };
}

const STATUS_WORD: Record<JiraSyncState, string> = {
  idle: SETTINGS_COPY.statusIdle,
  running: SETTINGS_COPY.statusRunning,
  succeeded: SETTINGS_COPY.statusSucceeded,
  partial: SETTINGS_COPY.statusPartial,
  failed: SETTINGS_COPY.statusFailed,
  skipped: SETTINGS_COPY.statusSkipped,
};
export const statusWord = (status: JiraSyncState): string => STATUS_WORD[status];

export type StatusTone = "neutral" | "pending" | "success" | "warning" | "danger";
const STATUS_TONE: Record<JiraSyncState, StatusTone> = {
  idle: "neutral",
  running: "pending",
  succeeded: "success",
  partial: "warning",
  failed: "danger",
  skipped: "neutral",
};
export const statusTone = (status: JiraSyncState): StatusTone => STATUS_TONE[status];

/** The tab reloads while a pass runs (ui.md, status line). */
export const POLL_MS = 5000;

/** True while the status says a pass runs: the tab reloads then. */
export const isRunning = (s: JiraSettings | "failed" | null): boolean => typeof s === "object" && s?.sync?.status === "running";

/** Calls `load` every `POLL_MS` while `running`; returns the stop function (none when not running). */
export function startPolling(running: boolean, load: () => void): (() => void) | undefined {
  if (!running) return undefined;
  const timer = setInterval(load, POLL_MS);
  return () => {
    clearInterval(timer);
  };
}

/** The typed keys are the saved target (nothing to check or save). */
export function isSavedTarget(s: JiraSettings, project: string, epic: string): boolean {
  const t = targetOf(project, epic);
  return s.jiraProject !== null && s.jiraProject === t.jiraProject && s.epicKey === t.epicKey;
}

export type Busy = "check" | "save" | "switch";

/** The tab's state. `run` is the token of the latest edit or call: a check or save answer with an older token is stale. */
export interface TabState {
  settings: JiraSettings | "failed" | null;
  /** The caller's Atlassian connection, read with the settings. */
  atlassian: AtlassianConnection | null;
  project: string;
  epic: string;
  outcome: Outcome | null;
  busy: Busy | null;
  run: number;
  seeded: boolean;
}
export const INITIAL_TAB: TabState = {
  settings: null,
  atlassian: null,
  project: "",
  epic: "",
  outcome: null,
  busy: null,
  run: 0,
  seeded: false,
};

const NOT_CONNECTED = { connected: false, status: null, accountName: null, connectedAt: null, lastUsedAt: null } as const;

export type TabAction =
  | { type: "loaded"; settings: JiraSettings; atlassian: AtlassianConnection }
  | { type: "load_failed" }
  | { type: "retry" }
  | { type: "edit"; field: "project" | "epic"; value: string; run: number }
  | { type: "start"; kind: Busy; run: number }
  | { type: "checked"; run: number; access: JiraAccess }
  | { type: "saved"; run: number; settings: JiraSettings; target: JiraTarget }
  | { type: "switched"; settings: JiraSettings }
  | { type: "disconnected" }
  | { type: "failed"; kind: Busy; run: number; failure: Failure };

/** What a failed call writes: the notice for `unavailable`, the texts under the fields it names (only when not stale). */
function afterFailure(st: TabState, f: Failure, run: number): Pick<TabState, "settings" | "outcome"> {
  if (f.kind === "unavailable" && typeof st.settings === "object" && st.settings !== null) {
    return { settings: { ...st.settings, available: false }, outcome: st.outcome };
  }
  if (f.kind === "fields" && run === st.run) return { settings: st.settings, outcome: { kind: "problem", texts: f.texts } };
  if (f.kind === "disconnected" && run === st.run) return { settings: st.settings, outcome: { kind: "problem", texts: { alert: f.text } } };
  return { settings: st.settings, outcome: st.outcome };
}

/** The first load seeds the typed keys with the saved target; later loads keep what the owner typed. */
function seedOf(st: TabState, s: JiraSettings): Pick<TabState, "seeded" | "project" | "epic"> {
  return st.seeded
    ? { seeded: true, project: st.project, epic: st.epic }
    : { seeded: true, project: s.jiraProject ?? "", epic: s.epicKey ?? "" };
}

/**
 * An answer always clears the `busy` of its own call and a save or switch answer always replaces the settings (the server's word);
 * only what the answer would write into the form (the outcome, the typed keys) needs the token to be the latest.
 */
export function tabReducer(st: TabState, a: TabAction): TabState {
  // A check holds `busy` only until an edit or a newer call; so only its own latest answer clears it.
  const done = (kind: Busy, run = st.run): Busy | null => (st.busy === kind && (kind !== "check" || run === st.run) ? null : st.busy);
  switch (a.type) {
    case "loaded":
      return { ...st, settings: a.settings, atlassian: a.atlassian, ...seedOf(st, a.settings) };
    case "load_failed":
      return st.settings === null || st.settings === "failed" ? { ...st, settings: "failed" } : st;
    case "retry":
      return { ...st, settings: null };
    case "edit":
      return {
        ...st,
        [a.field]: a.value,
        outcome: null,
        run: a.run,
        busy: st.busy === "check" ? null : st.busy,
      };
    case "start":
      return { ...st, busy: a.kind, run: a.run };
    case "checked":
      return {
        ...st,
        busy: done("check", a.run),
        outcome: a.run === st.run ? outcomeOf(a.access, st.atlassian?.mode === "oauth") : st.outcome,
      };
    case "saved": {
      const fresh = a.run === st.run;
      return {
        ...st,
        settings: a.settings,
        busy: done("save"),
        project: fresh ? (a.settings.jiraProject ?? a.target.jiraProject) : st.project,
        epic: fresh ? (a.settings.epicKey ?? "") : st.epic,
        outcome: fresh ? null : st.outcome,
      };
    }
    case "switched":
      return { ...st, settings: a.settings, busy: done("switch") };
    case "disconnected":
      // Not connected now; the tab reads the settings (their acting line) again.
      return { ...st, atlassian: st.atlassian === null ? null : { ...st.atlassian, ...NOT_CONNECTED }, outcome: null };
    case "failed":
      return { ...st, ...afterFailure(st, a.failure, a.run), busy: done(a.kind, a.run) };
  }
}

const STEP_TEXT: Record<JiraSyncStep, string> = {
  issues: SETTINGS_COPY.stepIssues,
  links: SETTINGS_COPY.stepLinks,
  order: SETTINGS_COPY.stepOrder,
};

/** What a running pass shows: the step text, "23 of 60", and the bar's numbers (`done` clamped into 0..total; `total` 0 has no bar value). */
export function progressView(p: JiraSyncProgress): { step: string; count: string; done: number; total: number } {
  const done = Math.min(p.done, p.total);
  return { step: STEP_TEXT[p.step], count: countText(done, p.total), done, total: p.total };
}

// ---- The Atlassian connection (docs/jira-sync/oauth.md#jira-tab) ----

/** The caller's connection row: shown to owners in the `oauth` mode only (none = the tab as in the `token` mode). */
export type ConnectRow = { kind: "connected"; name: string } | { kind: "expired" } | { kind: "none" };

export function connectRowOf(settings: JiraSettings, atlassian: AtlassianConnection | null): ConnectRow | null {
  if (!settings.editable || atlassian?.mode !== "oauth") return null;
  if (atlassian.status === "active") return { kind: "connected", name: atlassian.accountName ?? "" };
  return atlassian.status === "expired" ? { kind: "expired" } : { kind: "none" };
}

/** Why Check access, Save and the Sync switch are off for this owner (no active connection in the `oauth` mode), or null. */
export function connectGate(row: ConnectRow | null): string | null {
  if (row === null || row.kind === "connected") return null;
  return row.kind === "expired" ? SETTINGS_COPY.needsReconnect : SETTINGS_COPY.needsConnect;
}

/** The acting account line under the target: who the sync acts as, and the warning when that connection is expired or missing. */
export interface ActingLine {
  text: string;
  warning: string | null;
}

export function actingLineOf(acting: JiraActing | null, editable: boolean): ActingLine | null {
  if (acting === null) return null;
  const who = acting.connectedByMe ? SETTINGS_COPY.actingYou : (acting.connectedByName ?? "");
  if (acting.status === "missing" || who === "") {
    return { text: SETTINGS_COPY.actingNobody, warning: editable ? SETTINGS_COPY.actingSaveToSync : SETTINGS_COPY.actingOwnerSaves };
  }
  const text = `${SETTINGS_COPY.actingAs} ${who}`;
  if (acting.status === "active") return { text, warning: null };
  if (acting.connectedByMe) return { text, warning: SETTINGS_COPY.actingYouReconnect };
  const takeOver = editable ? ` ${SETTINGS_COPY.actingSaveToSync}` : "";
  return { text, warning: `${who} ${SETTINGS_COPY.actingMustReconnect}${takeOver}` };
}

/** True when an owner may save the unchanged target to take it over (it does not act as them, actively, yet). */
export const canTakeOver = (acting: JiraActing | null): boolean => acting !== null && !(acting.connectedByMe && acting.status === "active");

const ATLASSIAN_TOAST: Record<string, string> = {
  atlassian_consent_denied: COPY.atlassianConsentDenied,
  atlassian_state_invalid: COPY.atlassianStateInvalid,
  atlassian_code_invalid: COPY.atlassianCodeInvalid,
  atlassian_site_not_granted: COPY.atlassianSiteNotGranted,
  atlassian_unreachable: COPY.atlassianUnreachable,
  atlassian_oauth_unavailable: COPY.atlassianUnavailable,
};

/** The one toast of `?atlassian=<code>` (the callback's answer): `connected` succeeds, any other value is a failure. */
export function atlassianToast(code: string): { title: string; tone: "success" | "danger" } {
  if (code === "connected") return { title: COPY.atlassianConnected, tone: "success" };
  return { title: Object.hasOwn(ATLASSIAN_TOAST, code) ? ATLASSIAN_TOAST[code] : COPY.atlassianUnexpected, tone: "danger" };
}

/** Atlassian's page of connected apps: 3LO has no revoke endpoint, so the user removes Specflow's grant there. */
export const ATLASSIAN_GRANTS_URL = "https://id.atlassian.com/manage-profile/apps";
