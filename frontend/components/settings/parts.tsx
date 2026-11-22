"use client";
// docs/projects/ui.md#general-tab: the pieces of the General tab. Each takes plain props and has no data access; GeneralTab wires them to
// the ports. Design-system components only: TextField, Button, Avatar, Tag, Tooltip, Modal.
import { useId, type KeyboardEvent, type ReactNode } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Button } from "@/components/ui/Button";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { Tag } from "@/components/ui/Tag";
import { TextField } from "@/components/ui/TextField";
import { Tooltip } from "@/components/ui/Tooltip";
import { cx, nonEmpty } from "@/components/ui/utils";
import { COPY } from "@/lib/copy";
import type { MembersState, Suggestion } from "@/lib/domain/members";
import { addRowKey, confirmText, rowsOf, type Confirm, type Row } from "./settings-logic";
import { countText, SETTINGS_COPY } from "./settings-copy";
import "./settings.css";

export function NameBlock({
  value,
  error,
  canSave,
  busy,
  onChange,
  onSave,
}: Readonly<{ value: string; error?: string; canSave: boolean; busy: boolean; onChange: (v: string) => void; onSave: () => void }>) {
  return (
    <form
      className="settings-sec settings-inline settings-name"
      data-testid="settings-name"
      onSubmit={(e) => {
        e.preventDefault();
        if (canSave && !busy) onSave();
      }}
    >
      <TextField
        label={COPY.settingsProjectName}
        value={value}
        error={error}
        onChange={(e) => {
          onChange(e.target.value);
        }}
      />
      <Button variant="glass" type="submit" disabled={!canSave || busy} data-testid="settings-save">
        {COPY.settingsSave}
      </Button>
    </form>
  );
}

export interface AddRowViewProps {
  text: string;
  error?: string;
  items: Suggestion[];
  /** The list is wanted open (typing or focus); it shows only when it also has items. */
  open: boolean;
  active: number;
  busy: boolean;
  onText: (v: string) => void;
  onActive: (i: number) => void;
  onOpen: (open: boolean) => void;
  onPick: (s: Suggestion) => void;
  onAdd: () => void;
}

/** The email field, its suggestions and Add. Enter picks the active suggestion while the list is open, otherwise it adds. */
export function AddRowView(p: Readonly<AddRowViewProps>) {
  const id = useId();
  const listId = `${id}-l`;
  const showList = p.open && p.items.length > 0;
  const active = Math.min(p.active, Math.max(p.items.length - 1, 0));

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    const act = addRowKey(e.key, { active, count: p.items.length, showList, busy: p.busy });
    if (act === null) return;
    if (act.kind === "close") {
      e.nativeEvent.stopImmediatePropagation(); // closes the list only, whatever else listens for Esc on the document
      p.onOpen(false);
      return;
    }
    e.preventDefault();
    if (act.kind === "move") {
      if (act.open) p.onOpen(true);
      p.onActive(act.index);
    } else if (act.kind === "pick") {
      const picked = p.items.at(act.index);
      if (picked !== undefined) p.onPick(picked);
    } else p.onAdd();
  }

  return (
    <div className="settings-inline settings-add" data-testid="settings-add">
      <TextField
        label={COPY.collaboratorEmail}
        placeholder={SETTINGS_COPY.emailPlaceholder}
        value={p.text}
        error={p.error}
        type="email"
        inputMode="email"
        role="combobox"
        aria-expanded={showList}
        aria-controls={showList ? listId : undefined}
        aria-activedescendant={showList ? `${id}-o${String(active)}` : undefined}
        aria-autocomplete="list"
        onChange={(e) => {
          p.onText(e.target.value);
          p.onOpen(true);
          p.onActive(0);
        }}
        onFocus={() => {
          p.onOpen(true);
        }}
        onBlur={() => {
          p.onOpen(false);
        }}
        onKeyDown={onKey}
      />
      <Button type="button" disabled={p.busy || p.text.trim() === ""} onClick={p.onAdd} data-testid="settings-add-button">
        {COPY.collaboratorAdd}
      </Button>
      {showList ? (
        <GlassSurface
          variant="strong"
          floating
          className="gdg-pp__list settings-suggest"
          role="listbox"
          id={listId}
          aria-label={SETTINGS_COPY.suggestionsLabel}
        >
          {p.items.map((s, i) => (
            <div
              key={s.email}
              id={`${id}-o${String(i)}`}
              role="option"
              tabIndex={-1}
              aria-selected={i === active}
              className={cx("gdg-pp__opt", i === active && "is-active")}
              onMouseDown={(e) => {
                e.preventDefault(); // keeps the focus in the field, so the blur does not close the list before the pick
                p.onPick(s);
              }}
              onMouseEnter={() => {
                p.onActive(i);
              }}
            >
              <Avatar name={s.name} size="sm" title={false} />
              <span>
                <b>{s.name}</b>
                <small>{s.email}</small>
              </span>
            </div>
          ))}
        </GlassSurface>
      ) : null}
    </div>
  );
}

function RowFace({ row }: Readonly<{ row: Row }>) {
  if (row.type === "invite") {
    return (
      <span className="gdg-avatar gdg-avatar--md gdg-avatar--neutral" role="img" aria-label={row.title}>
        <Icon name="at" size={16} />
      </span>
    );
  }
  return <Avatar name={row.title} size="md" title={false} />;
}

/** The people card. `onAction` is set only in the owner view; a row without an action has no x. */
export function PeopleList({ rows, onAction }: Readonly<{ rows: Row[]; onAction?: (row: Row) => void }>) {
  return (
    <GlassSurface as="ul" className="settings-list" aria-label={SETTINGS_COPY.listLabel} data-testid="settings-list">
      {rows.map((row) => (
        <li key={row.key} className="settings-row" data-testid="settings-row" data-kind={row.type}>
          <RowFace row={row} />
          <div className="settings-who">
            <b title={row.title}>{row.title}</b>
            {nonEmpty(row.subtitle) !== undefined ? <small title={row.subtitle}>{row.subtitle}</small> : null}
          </div>
          {row.badge === "owner" ? <Tag>{COPY.badgeOwner}</Tag> : null}
          {row.badge === "invited" ? <Tag tone="warning">{COPY.badgeInvited}</Tag> : null}
          {row.action !== null && onAction !== undefined ? (
            <Tooltip label={row.action.kind === "cancel" ? SETTINGS_COPY.cancelInvite : SETTINGS_COPY.remove} side="left">
              <button
                type="button"
                className="gdg-iconbtn"
                aria-label={`${row.action.kind === "cancel" ? SETTINGS_COPY.cancelInvite : SETTINGS_COPY.remove} ${row.title}`}
                data-testid="settings-row-x"
                onClick={() => {
                  onAction(row);
                }}
              >
                <Icon name="x" size={16} />
              </button>
            </Tooltip>
          ) : null}
        </li>
      ))}
    </GlassSurface>
  );
}

export function LeaveBlock({ onLeave }: Readonly<{ onLeave: () => void }>) {
  return (
    <div className="settings-leave">
      <Button variant="glass" size="sm" onClick={onLeave} data-testid="settings-leave">
        <Icon name="sign-out" size={16} />
        {COPY.leaveProject}
      </Button>
    </div>
  );
}

/** The confirmation of remove, cancel and leave. The first focus lands on the dialog, not on the destructive button. */
export function ConfirmDialog({
  confirm,
  busy,
  onConfirm,
  onClose,
}: Readonly<{ confirm: Confirm | null; busy: boolean; onConfirm: () => void; onClose: () => void }>) {
  if (confirm === null) return null;
  const t = confirmText(confirm);
  return (
    <Modal
      size="sm"
      title={t.title}
      onClose={onClose}
      closeDisabled={busy}
      initialFocus="dialog"
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy} data-testid="settings-confirm-cancel">
            {t.dismiss}
          </Button>
          <Button onClick={onConfirm} disabled={busy} data-testid="settings-confirm">
            {t.confirm}
          </Button>
        </>
      }
    >
      <p className="settings-dialog__body" data-testid="settings-dialog">
        {t.body}
      </p>
    </Modal>
  );
}

/** Shown when the tab could not be read (the list by default); "Try again" reads it again. */
export function LoadFailed({ onRetry, text = SETTINGS_COPY.loadFailed }: Readonly<{ onRetry: () => void; text?: string }>) {
  return (
    <div className="settings-note" role="alert" data-testid="settings-failed">
      <span>{text}</span>
      <Button variant="glass" size="sm" onClick={onRetry}>
        {SETTINGS_COPY.tryAgain}
      </Button>
    </div>
  );
}

export interface GeneralViewProps {
  state: MembersState;
  draft: string;
  nameError?: string;
  canSave: boolean;
  saving: boolean;
  confirm: Confirm | null;
  confirming: boolean;
  /** The add row (it keeps its own typing state); shown to the owner only. */
  addSlot: ReactNode;
  onDraft: (v: string) => void;
  onSave: () => void;
  onRow: (row: Row) => void;
  onLeave: () => void;
  onConfirm: () => void;
  onDismiss: () => void;
}

/** The General tab for the caller's role (`state.role`): the owner's controls, or the collaborator's read-only list and Leave project. */
export function GeneralView(p: Readonly<GeneralViewProps>) {
  const isOwner = p.state.role === "owner";
  return (
    <>
      {isOwner ? (
        <NameBlock value={p.draft} error={p.nameError} canSave={p.canSave} busy={p.saving} onChange={p.onDraft} onSave={p.onSave} />
      ) : null}
      <section className="settings-sec" aria-labelledby="settings-collab-h">
        <div className="settings-sec__head">
          <h2 className="title-2 settings-sec__title" id="settings-collab-h">
            {COPY.collaboratorsHeading}
          </h2>
          <span className="settings-count" data-testid="settings-count">
            {countText(p.state.members.length + p.state.invites.length, p.state.limit)}
          </span>
        </div>
        {isOwner ? p.addSlot : null}
        <PeopleList rows={rowsOf(p.state, isOwner)} onAction={isOwner ? p.onRow : undefined} />
      </section>
      {isOwner ? null : <LeaveBlock onLeave={p.onLeave} />}
      <ConfirmDialog confirm={p.confirm} busy={p.confirming} onConfirm={p.onConfirm} onClose={p.onDismiss} />
    </>
  );
}
