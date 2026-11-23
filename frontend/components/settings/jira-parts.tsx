"use client";
// docs/jira-sync/ui.md#jira-tab: the pieces of the Jira tab. Each takes plain props and has no data access; JiraTab wires them to the
// port. Design-system components only: TextField, Button, Switch, Tag.
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Switch } from "@/components/ui/Switch";
import { Tag } from "@/components/ui/Tag";
import { TextField } from "@/components/ui/TextField";
import { COPY } from "@/lib/copy";
import type { JiraSettings, JiraSync } from "@/lib/domain/jira";
import { isSavedTarget, PHASE_LABEL, reasonText, statusTone, statusWord, targetOf, type Outcome, type Phase } from "./jira-logic";
import { JiraProgress } from "./JiraProgress";
import { SETTINGS_COPY } from "./settings-copy";
import "./settings.css";

const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

/** A time the API sent; a text that is not a date is shown as it is (the format would throw). */
const whenText = (iso: string): string => (Number.isNaN(Date.parse(iso)) ? iso : WHEN.format(new Date(iso)));

/** What a passed check found: the Jira project and the Epic that will be used. */
function CheckOk({
  outcome,
  epicKey,
  projectName,
}: Readonly<{ outcome: Extract<Outcome, { kind: "ok" }>; epicKey: string | null; projectName: string | null }>) {
  const created = projectName === null ? `${SETTINGS_COPY.epicCreated} the project.` : `${SETTINGS_COPY.epicCreated} ${projectName}`;
  return (
    <p className="settings-hint settings-check" role="status" data-testid="jira-check">
      <span>{`${SETTINGS_COPY.jiraProjectChecked} ${outcome.projectName}`}</span>
      <span>{epicKey === null ? created : `${SETTINGS_COPY.epicChecked} ${epicKey}: ${outcome.epicSummary ?? ""}`}</span>
    </p>
  );
}

/** The status word, when the pass ended and why it did not succeed; while it runs and has started, its step and count with a bar. */
function StatusLine({ sync }: Readonly<{ sync: JiraSync | null }>) {
  // The live region stays mounted (empty without a sync) so a polled change of the word is announced.
  return (
    <section className="settings-status" role="status" aria-label={SETTINGS_COPY.statusLabel} data-testid="jira-status">
      {sync === null ? null : (
        <>
          <Tag tone={statusTone(sync.status)}>{statusWord(sync.status)}</Tag>
          {sync.finishedAt === null ? null : <span className="settings-hint">{whenText(sync.finishedAt)}</span>}
          {sync.errorCode === null ? null : <span className="settings-hint">{reasonText(sync.errorCode)}</span>}
          {sync.status === "running" && sync.progress !== null ? <JiraProgress progress={sync.progress} /> : null}
        </>
      )}
    </section>
  );
}

export interface JiraViewProps {
  settings: JiraSettings;
  projectName: string | null;
  /** The typed keys (the form's own state). */
  project: string;
  epic: string;
  phase: Phase;
  outcome: Outcome | null;
  /** The Switch call is in flight. */
  switching: boolean;
  onProject: (v: string) => void;
  onEpic: (v: string) => void;
  onPrimary: () => void;
  onSwitch: (enabled: boolean) => void;
}

/** The Jira tab for the caller's role (`settings.editable`): the owner's controls, or the same blocks read-only without the button. */
export function JiraView(p: Readonly<JiraViewProps>) {
  const s = p.settings;
  const locked = !s.editable || !s.available;
  const texts = p.outcome?.kind === "problem" ? p.outcome.texts : {};
  const busy = p.phase === "checking" || p.phase === "saving";
  const created = s.epicKey === null ? s.createdEpicKey : null;
  // Nothing to check or save: the typed keys are the saved target and no answer is waiting.
  const unchanged = p.outcome === null && isSavedTarget(s, p.project, p.epic);
  return (
    <>
      {s.available ? null : (
        <p className="settings-note" role="note" data-testid="jira-unavailable">
          <Icon name="plug" size={20} />
          {COPY.jiraUnavailable}
        </p>
      )}
      <form
        className="settings-sec settings-jira"
        data-testid="jira-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!locked && !busy && !unchanged && p.project.trim() !== "") p.onPrimary();
        }}
      >
        <TextField
          label={SETTINGS_COPY.jiraProject}
          placeholder={SETTINGS_COPY.jiraProjectPlaceholder}
          value={p.project}
          error={texts.project}
          disabled={locked}
          onChange={(e) => {
            p.onProject(e.target.value);
          }}
        />
        <TextField
          label={SETTINGS_COPY.epicKey}
          placeholder={SETTINGS_COPY.epicKeyPlaceholder}
          help={SETTINGS_COPY.epicKeyHint}
          value={p.epic}
          error={texts.epic}
          disabled={locked}
          onChange={(e) => {
            p.onEpic(e.target.value);
          }}
        />
        {s.editable ? (
          <Button type="submit" disabled={locked || busy || p.project.trim() === "" || unchanged} data-testid="jira-primary">
            {PHASE_LABEL[p.phase]}
          </Button>
        ) : null}
      </form>
      {p.outcome?.kind === "ok" ? (
        <CheckOk outcome={p.outcome} epicKey={targetOf(p.project, p.epic).epicKey} projectName={p.projectName} />
      ) : null}
      {texts.alert === undefined ? null : (
        <p className="settings-alert" role="alert" data-testid="jira-alert">
          {texts.alert}
        </p>
      )}
      <div className="settings-sec settings-switch">
        <Switch
          label={SETTINGS_COPY.syncSwitch}
          checked={s.enabled}
          busy={p.switching}
          disabled={locked || s.jiraProject === null}
          onChange={p.onSwitch}
        />
        <span className="settings-hint">{SETTINGS_COPY.syncHint}</span>
      </div>
      {created !== null ? (
        <p className="settings-hint" data-testid="jira-created-epic">
          {SETTINGS_COPY.epicCreatedBy}{" "}
          {s.siteUrl === null ? (
            created
          ) : (
            <a
              className="settings-link"
              href={`${s.siteUrl}/browse/${encodeURIComponent(created)}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              {created}
            </a>
          )}
        </p>
      ) : null}
      <StatusLine sync={s.sync} />
    </>
  );
}
