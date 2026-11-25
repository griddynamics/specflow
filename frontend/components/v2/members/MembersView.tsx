"use client";
// The body of the Members dialog (README "Header"): what Settings > General does today, in the Graphico look. The owner renames the project,
// adds people by email with suggestions, removes members and cancels invites, each after a confirmation; a collaborator sees the list
// read-only and can leave. The state is `use-members-controller.ts` and `use-add-row.ts`; this file only draws it.
import { useId } from "react";
import { Button, ConfirmPanel, DialogFoot, Field, Notice, PersonList, PersonRow, Split, SuggestionList, TextInput } from "@graphico/ui";
import { COPY } from "@/lib/copy";
import type { MembersState } from "@/lib/domain/members";
import { activeOf, handleAddRowKey, showsList } from "@/components/settings/add-row-keys";
import { countText, SETTINGS_COPY } from "@/components/settings/settings-copy";
import { confirmText, rowsOf, type Confirm, type Row } from "@/components/settings/settings-logic";
import { useAddRow, type AddOutcome } from "@/components/settings/use-add-row";
import { V2_COPY } from "../v2-copy";

export interface MembersViewProps {
  state: MembersState;
  draft: string;
  nameError?: string;
  canSave: boolean;
  saving: boolean;
  confirm: Confirm | null;
  confirming: boolean;
  projectId: string;
  addKey: number;
  onAdd: (text: string) => Promise<AddOutcome>;
  onDraft: (v: string) => void;
  onSave: () => void;
  onRow: (row: Row) => void;
  onLeave: () => void;
  onConfirm: () => void;
  onDismiss: () => void;
  onDone: () => void;
}

const rowAction = (row: Row, onRow: (row: Row) => void) =>
  row.action === null
    ? undefined
    : {
        label: `${row.type === "invite" ? SETTINGS_COPY.cancelInvite : SETTINGS_COPY.remove} ${row.title}`,
        onClick: () => {
          onRow(row);
        },
      };

const TAGS = { owner: V2_COPY.owner, invited: V2_COPY.invited } as const;
const tagOf = (row: Row): string | undefined => (row.badge === null ? undefined : TAGS[row.badge]);

/** The email field with its suggestions and Add. A success remounts it (new key), which empties the field. */
function AddByEmail({ projectId, onAdd }: Readonly<{ projectId: string; onAdd: (text: string) => Promise<AddOutcome> }>) {
  const row = useAddRow(projectId, onAdd);
  const id = useId();
  const listed = showsList(row);
  const active = activeOf(row);
  return (
    <Split>
      <Field label={COPY.collaboratorEmail} htmlFor={id} error={row.error}>
        <TextInput
          id={id}
          type="email"
          inputMode="email"
          role="combobox"
          aria-expanded={listed}
          aria-autocomplete="list"
          value={row.text}
          onChange={(e) => {
            row.type(e.target.value);
            row.setOpen(true);
            row.setActive(0);
          }}
          onFocus={() => {
            row.setOpen(true);
          }}
          onKeyDown={(e) => {
            handleAddRowKey(e, { ...row, onOpen: row.setOpen, onActive: row.setActive, onPick: row.pick, onAdd: row.add });
          }}
        />
      </Field>
      <Button disabled={row.busy || row.text.trim() === ""} onClick={row.add}>
        {COPY.collaboratorAdd}
      </Button>
      {listed ? (
        <SuggestionList
          label={V2_COPY.suggestions}
          items={row.items.map((s, n) => ({
            key: s.email,
            name: s.name,
            email: s.email,
            onPick: () => {
              row.pick(s);
            },
            active: n === active,
          }))}
        />
      ) : null}
    </Split>
  );
}

export function MembersView(p: Readonly<MembersViewProps>) {
  const nameId = useId();
  const isOwner = p.state.role === "owner";
  const rows = rowsOf(p.state, isOwner);
  const youKeys = new Set(p.state.members.filter((m) => m.isYou).map((m) => `m:${m.userId}`));
  const confirm = p.confirm === null ? null : confirmText(p.confirm);
  return (
    <>
      {isOwner ? (
        <Split>
          <Field label={V2_COPY.nameLabel} htmlFor={nameId} error={p.nameError}>
            <TextInput
              id={nameId}
              value={p.draft}
              maxLength={200}
              onChange={(e) => {
                p.onDraft(e.target.value);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && p.canSave && !p.saving) {
                  e.preventDefault();
                  p.onSave();
                }
              }}
            />
          </Field>
          <Button variant="ghost" disabled={!p.canSave || p.saving} onClick={p.onSave}>
            {COPY.settingsSave}
          </Button>
        </Split>
      ) : null}
      <Notice>{countText(p.state.members.length + p.state.invites.length, p.state.limit)}</Notice>
      {isOwner ? <AddByEmail key={p.addKey} projectId={p.projectId} onAdd={p.onAdd} /> : null}
      <PersonList label={V2_COPY.membersLabel}>
        {rows.map((row) => (
          <PersonRow
            key={row.key}
            name={row.title}
            detail={row.type === "invite" ? V2_COPY.inviteDetail : row.subtitle}
            invited={row.type === "invite"}
            youLabel={youKeys.has(row.key) ? V2_COPY.you : undefined}
            tag={tagOf(row)}
            action={rowAction(row, p.onRow)}
          />
        ))}
      </PersonList>
      {confirm === null ? null : (
        <ConfirmPanel
          title={confirm.title}
          body={confirm.body}
          confirmLabel={confirm.confirm}
          keepLabel={confirm.dismiss}
          busy={p.confirming}
          onConfirm={p.onConfirm}
          onKeep={p.onDismiss}
        />
      )}
      {isOwner || confirm !== null ? null : (
        <div>
          <Button variant="ghost" size="sm" onClick={p.onLeave}>
            {COPY.leaveProject}
          </Button>
        </div>
      )}
      <DialogFoot>
        <span />
        <Button variant="ghost" onClick={p.onDone}>
          {V2_COPY.done}
        </Button>
      </DialogFoot>
    </>
  );
}

export function MembersNotice({ failed }: Readonly<{ failed: boolean }>) {
  return <Notice role={failed ? "alert" : "status"}>{failed ? SETTINGS_COPY.loadFailed : V2_COPY.loading}</Notice>;
}
