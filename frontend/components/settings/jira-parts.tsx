"use client";
// docs/jira-sync/ui.md#jira-tab, oauth.md#jira-tab: the pieces of the Jira tab. Each takes plain props and has no data access; JiraTab
// wires them to the port. Design-system components only: TextField, Button, Switch, Tag, GlassSurface, Modal.
import { logoSrc } from "@/components/connections/logos";
import { Button } from "@/components/ui/Button";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { Switch } from "@/components/ui/Switch";
import { Tag } from "@/components/ui/Tag";
import { TextField } from "@/components/ui/TextField";
import { COPY } from "@/lib/copy";
import type { AtlassianConnection, JiraSettings, JiraSync } from "@/lib/domain/jira";
import {
  actingLineOf,
  ATLASSIAN_GRANTS_URL,
  canTakeOver,
  connectGate,
  connectRowOf,
  isSavedTarget,
  PHASE_LABEL,
  statusTone,
  statusWord,
  syncErrorText,
  targetOf,
  type ActingLine,
  type ConnectRow,
  type Outcome,
  type Phase,
} from "./jira-logic";
import { JiraProgress } from "./JiraProgress";
import { SETTINGS_COPY } from "./settings-copy";
import "./settings.css";

const noop = () => undefined;
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
function StatusLine({ sync, oauth }: Readonly<{ sync: JiraSync | null; oauth: boolean }>) {
  // The live region stays mounted (empty without a sync) so a polled change of the word is announced.
  return (
    <section className="settings-status" role="status" aria-label={SETTINGS_COPY.statusLabel} data-testid="jira-status">
      {sync === null ? null : (
        <>
          <Tag tone={statusTone(sync.status)}>{statusWord(sync.status)}</Tag>
          {sync.finishedAt === null ? null : <span className="settings-hint">{whenText(sync.finishedAt)}</span>}
          {sync.errorCode === null ? null : <span className="settings-hint">{syncErrorText(sync.errorCode, oauth)}</span>}
          {sync.status === "running" && sync.progress !== null ? <JiraProgress progress={sync.progress} /> : null}
        </>
      )}
    </section>
  );
}

/** "Connected apps" at Atlassian: where the user removes Specflow's grant (3LO has no revoke endpoint). */
function GrantNote() {
  return (
    <p className="settings-hint" data-testid="atlassian-grant-note">
      {SETTINGS_COPY.atlassianGrantNote}{" "}
      <a className="settings-link" href={ATLASSIAN_GRANTS_URL} target="_blank" rel="noopener noreferrer">
        {SETTINGS_COPY.atlassianGrantLink}
      </a>
      .
    </p>
  );
}

const JIRA_MARK = logoSrc("jira");

/** The owner's own Atlassian connection (`oauth` mode): connected with Disconnect, expired with Reconnect, or Connect Atlassian. */
export function ConnectionRow({
  row,
  connectedAt,
  busy,
  onConnect,
  onDisconnect,
}: Readonly<{ row: ConnectRow; connectedAt: string | null; busy: boolean; onConnect: () => void; onDisconnect: () => void }>) {
  let title: string;
  let hint: string;
  if (row.kind === "connected") {
    title = `${SETTINGS_COPY.atlassianConnectedAs} ${row.name}`;
    hint = connectedAt === null ? "" : `${SETTINGS_COPY.atlassianConnectedOn} ${whenText(connectedAt)}`;
  } else if (row.kind === "expired") {
    title = SETTINGS_COPY.atlassianExpired;
    hint = SETTINGS_COPY.atlassianExpiredHint;
  } else {
    title = SETTINGS_COPY.atlassianNone;
    hint = SETTINGS_COPY.atlassianNoneHint;
  }
  return (
    <section className="settings-sec" aria-label={SETTINGS_COPY.atlassianLabel} data-testid="atlassian-row" data-state={row.kind}>
      <GlassSurface className="settings-list settings-atlassian">
        <div className="settings-row">
          <span className="gdg-avatar gdg-avatar--md gdg-avatar--neutral settings-atlassian__mark" aria-hidden>
            {row.kind === "expired" ? <Icon name="warning-circle" size={20} /> : <img src={JIRA_MARK} alt="" width={18} height={18} />}
          </span>
          <div className="settings-who">
            <b>{title}</b>
            {hint === "" ? null : <small>{hint}</small>}
          </div>
          {row.kind === "connected" ? (
            <Button variant="glass" size="sm" disabled={busy} onClick={onDisconnect} data-testid="atlassian-disconnect">
              {SETTINGS_COPY.atlassianDisconnect}
            </Button>
          ) : (
            <Button size="sm" disabled={busy} onClick={onConnect} data-testid="atlassian-connect">
              {row.kind === "expired" ? SETTINGS_COPY.atlassianReconnect : SETTINGS_COPY.atlassianConnect}
            </Button>
          )}
        </div>
      </GlassSurface>
      {row.kind === "none" ? null : <GrantNote />}
    </section>
  );
}

/** Confirms Disconnect. The first focus lands on the dialog, not on the destructive button. */
export function DisconnectDialog({ busy, onConfirm, onClose }: Readonly<{ busy: boolean; onConfirm: () => void; onClose: () => void }>) {
  return (
    <Modal
      size="sm"
      title={SETTINGS_COPY.disconnectTitle}
      onClose={onClose}
      closeDisabled={busy}
      initialFocus="dialog"
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy} data-testid="atlassian-disconnect-cancel">
            {SETTINGS_COPY.keep}
          </Button>
          <Button onClick={onConfirm} disabled={busy} data-testid="atlassian-disconnect-confirm">
            {SETTINGS_COPY.atlassianDisconnect}
          </Button>
        </>
      }
    >
      <p className="settings-dialog__body">{SETTINGS_COPY.disconnectBody}</p>
      <GrantNote />
    </Modal>
  );
}

/** Why Check access and Save are off: the owner has no active Atlassian connection. */
function GateHint({ text }: Readonly<{ text: string | null }>) {
  return text === null ? null : (
    <p className="settings-hint" data-testid="jira-gate">
      {text}
    </p>
  );
}

/** Who the target syncs to Jira as, with the warning when that connection is expired or missing. */
function Acting({ line }: Readonly<{ line: ActingLine | null }>) {
  if (line === null) return null;
  return (
    <p className="settings-acting" data-testid="jira-acting" data-warning={line.warning !== null}>
      <span>{line.text}</span>
      {line.warning === null ? null : (
        <span className="settings-acting__warning" role="note">
          <Icon name="warning-circle" size={16} />
          {line.warning}
        </span>
      )}
    </p>
  );
}

export interface JiraViewProps {
  settings: JiraSettings;
  /** The caller's Atlassian connection; null (not read) shows the tab as in the `token` mode. */
  atlassian?: AtlassianConnection | null;
  projectName: string | null;
  /** The typed keys (the form's own state). */
  project: string;
  epic: string;
  phase: Phase;
  outcome: Outcome | null;
  /** The Switch call is in flight. */
  switching: boolean;
  /** Connect or Disconnect is in flight. */
  connecting?: boolean;
  onConnect?: () => void;
  onDisconnect?: () => void;
  onProject: (v: string) => void;
  onEpic: (v: string) => void;
  onPrimary: () => void;
  onSwitch: (enabled: boolean) => void;
}

/** The Jira tab for the caller's role (`settings.editable`): the owner's controls, or the same blocks read-only without the button. */
export function JiraView(p: Readonly<JiraViewProps>) {
  const s = p.settings;
  const row = connectRowOf(s, p.atlassian ?? null);
  const gate = s.available ? connectGate(row) : null; // the owner has no active Atlassian connection (oauth mode)
  const locked = !s.editable || !s.available || gate !== null;
  const texts = p.outcome?.kind === "problem" ? p.outcome.texts : {};
  const busy = p.phase === "checking" || p.phase === "saving";
  const created = s.epicKey === null ? s.createdEpicKey : null;
  // Nothing to check or save: the typed keys are the saved target, no answer is waiting and the target already acts as this owner.
  const unchanged = p.outcome === null && isSavedTarget(s, p.project, p.epic) && !canTakeOver(s.connection);
  const acting = actingLineOf(s.connection, s.editable);
  const gated = gate !== null;
  return (
    <>
      {row === null || !s.available ? null : (
        <ConnectionRow
          row={row}
          connectedAt={p.atlassian?.connectedAt ?? null}
          busy={p.connecting === true}
          onConnect={p.onConnect ?? noop}
          onDisconnect={p.onDisconnect ?? noop}
        />
      )}
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
          disabled={!s.editable || !s.available}
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
          disabled={!s.editable || !s.available}
          onChange={(e) => {
            p.onEpic(e.target.value);
          }}
        />
        {s.editable ? (
          <Button
            type="submit"
            variant={gated ? "glass" : "primary"}
            disabled={locked || busy || p.project.trim() === "" || unchanged}
            data-testid="jira-primary"
          >
            {PHASE_LABEL[p.phase]}
          </Button>
        ) : null}
        <GateHint text={gate} />
      </form>
      <Acting line={acting} />
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
      <StatusLine sync={s.sync} oauth={p.atlassian?.mode === "oauth"} />
    </>
  );
}
