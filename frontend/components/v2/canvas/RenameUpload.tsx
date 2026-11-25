"use client";
// Renaming an upload batch: a field with Save and Cancel. The server decides what a name may be (1 to 80 characters, trimmed); a refusal is said
// in words. On success the canvas is read again, which also renames the dialog.
import { useId, useState } from "react";
import { Button, Field, Row, Stack, TextInput } from "@graphico/ui";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { renameProblem } from "./node-files-logic";

export function RenameUpload({
  projectId,
  sourceId,
  current,
  onRenamed,
  onCancel,
}: Readonly<{ projectId: string; sourceId: string; current: string; onRenamed: () => void; onCancel: () => void }>) {
  const { sources } = useV2Ports();
  const id = useId();
  const [name, setName] = useState(current);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | undefined>(undefined);
  const save = () => {
    if (busy || name.trim() === "") return;
    setBusy(true);
    setProblem(undefined);
    sources.rename(projectId, sourceId, name).then(
      () => {
        onRenamed();
      },
      (e: unknown) => {
        setBusy(false);
        setProblem(renameProblem(e));
      },
    );
  };
  return (
    <Stack
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <Field label={CANVAS_COPY.renameLabel} htmlFor={id} error={problem}>
        <TextInput
          id={id}
          value={name}
          autoFocus
          spellCheck={false}
          readOnly={busy}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <Row>
        <Button type="submit" size="sm" disabled={busy || name.trim() === ""} aria-busy={busy || undefined}>
          {busy ? CANVAS_COPY.renameSaving : CANVAS_COPY.renameSave}
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
          {CANVAS_COPY.renameCancel}
        </Button>
      </Row>
    </Stack>
  );
}
