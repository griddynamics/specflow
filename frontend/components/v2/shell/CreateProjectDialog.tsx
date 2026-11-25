"use client";
// D11, D12: the "Create new project" dialog of the project menu, and the "Create your first project" window of a person with no project,
// which cannot be closed. The form is the name (the collaborators are invited from Members once the project exists); the rules and the
// failure texts are `components/create-project/form-logic.ts`. On success `onCreated` gets the new project's id (the caller opens its canvas).
import { useEffect, useRef, useState, type SubmitEvent } from "react";
import { Button, Dialog, DialogFoot, Field, Notice, Stack, TextInput } from "@graphico/ui";
import { buildInput, createFailure, initialForm, nameError } from "@/components/create-project/form-logic";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";

function CreateForm({
  firstRun,
  onClose,
  onCreated,
}: Readonly<{ firstRun: boolean; onClose: () => void; onCreated: (projectId: string) => void }>) {
  const { createProject } = useV2Ports();
  const [name, setName] = useState(initialForm().name);
  const [error, setError] = useState<string | undefined>(undefined);
  const [createFailed, setCreateError] = useState(false);
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const input = buildInput({ ...initialForm(), name });
    if (input === null) {
      setError(nameError(name) ?? undefined);
      return;
    }
    setBusy(true);
    setCreateError(false);
    createProject.create(input).then(
      (project) => {
        if (!live.current) return;
        onCreated(project.id);
      },
      (err: unknown) => {
        if (!live.current) return;
        setBusy(false);
        const f = createFailure(err, name);
        setError(f.nameMsg ?? undefined);
        setCreateError(f.alert);
      },
    );
  };

  return (
    <Stack onSubmit={submit}>
      <Notice>{V2_COPY.createIntro}</Notice>
      <Field label={V2_COPY.nameLabel} htmlFor="v2-project-name" error={error}>
        <TextInput
          id="v2-project-name"
          value={name}
          maxLength={200}
          autoFocus
          onChange={(e) => {
            setName(e.target.value);
            setError(undefined);
          }}
        />
      </Field>
      {createFailed ? <Notice role="alert">{V2_COPY.createFailed}</Notice> : null}
      <DialogFoot>
        {firstRun ? (
          <span />
        ) : (
          <Button variant="ghost" onClick={onClose}>
            {V2_COPY.cancel}
          </Button>
        )}
        <Button type="submit" disabled={busy || name.trim() === ""}>
          {V2_COPY.create}
        </Button>
      </DialogFoot>
    </Stack>
  );
}

export function CreateProjectDialog({
  open,
  firstRun = false,
  onClose,
  onCreated,
}: Readonly<{ open: boolean; firstRun?: boolean; onClose: () => void; onCreated: (projectId: string) => void }>) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      dismissible={!firstRun}
      title={firstRun ? V2_COPY.firstRunTitle : V2_COPY.newTitle}
      eyebrow={firstRun ? V2_COPY.firstRunEyebrow : V2_COPY.newEyebrow}
    >
      {open ? <CreateForm firstRun={firstRun} onClose={onClose} onCreated={onCreated} /> : null}
    </Dialog>
  );
}
