// docs/jira-sync/ui.md#jira-tab, #errors-shown: the rules of the Jira tab, free of React. The button phase, which text goes where after a
// failed check or save, the texts of the reason codes and the status word.
import { COPY } from "@/lib/copy";
import { isDataError, type DataError } from "@/lib/domain/errors";
import type { JiraAccess, JiraAccessField, JiraSettings, JiraSyncState, JiraTarget } from "@/lib/domain/jira";
import { SETTINGS_COPY } from "./settings-copy";

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
};

/** The text of an access-check reason or a sync `error_code`; `field` tells the two `jira_not_found` apart. */
export function reasonText(code: string, field: JiraAccessField | null = null): string {
  if (code === "jira_not_found") return field === "epic_key" ? COPY.jiraEpicNotFound : COPY.jiraProjectNotFound;
  return Object.hasOwn(REASON_TEXT, code) ? REASON_TEXT[code] : COPY.jiraUnexpectedError;
}

/** A check answer as the form's outcome: the reason sits under the field it names, or is the alert line. */
export function outcomeOf(access: JiraAccess): Outcome {
  if (access.result === "ok") return { kind: "ok", projectName: access.projectName, epicSummary: access.epicSummary };
  const text = reasonText(access.reason, access.field);
  if (access.field === "jira_project") return { kind: "problem", texts: { project: text } };
  return { kind: "problem", texts: access.field === "epic_key" ? { epic: text } : { alert: text } };
}

/** What a failed call means for the screen: `gone` (go home), `unavailable` (the notice), fields under the inputs, or a toast. */
export type Failure = { kind: "gone" } | { kind: "unavailable" } | { kind: "fields"; texts: Problem } | { kind: "toast"; text: string };

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
  project: string;
  epic: string;
  outcome: Outcome | null;
  busy: Busy | null;
  run: number;
  seeded: boolean;
}
export const INITIAL_TAB: TabState = { settings: null, project: "", epic: "", outcome: null, busy: null, run: 0, seeded: false };

export type TabAction =
  | { type: "loaded"; settings: JiraSettings }
  | { type: "load_failed" }
  | { type: "retry" }
  | { type: "edit"; field: "project" | "epic"; value: string; run: number }
  | { type: "start"; kind: Busy; run: number }
  | { type: "checked"; run: number; access: JiraAccess }
  | { type: "saved"; run: number; settings: JiraSettings; target: JiraTarget }
  | { type: "switched"; settings: JiraSettings }
  | { type: "failed"; kind: Busy; run: number; failure: Failure };

/** What a failed call writes: the notice for `unavailable`, the texts under the fields it names (only when not stale). */
function afterFailure(st: TabState, f: Failure, run: number): Pick<TabState, "settings" | "outcome"> {
  if (f.kind === "unavailable" && typeof st.settings === "object" && st.settings !== null) {
    return { settings: { ...st.settings, available: false }, outcome: st.outcome };
  }
  if (f.kind === "fields" && run === st.run) return { settings: st.settings, outcome: { kind: "problem", texts: f.texts } };
  return { settings: st.settings, outcome: st.outcome };
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
      return st.seeded
        ? { ...st, settings: a.settings }
        : { ...st, settings: a.settings, seeded: true, project: a.settings.jiraProject ?? "", epic: a.settings.epicKey ?? "" };
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
      return { ...st, busy: done("check", a.run), outcome: a.run === st.run ? outcomeOf(a.access) : st.outcome };
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
    case "failed":
      return { ...st, ...afterFailure(st, a.failure, a.run), busy: done(a.kind, a.run) };
  }
}
