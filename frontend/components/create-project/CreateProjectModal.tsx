"use client";
// FE-CRT-01, FE-CRT-02, FE-CRT-06, FE-DEMO-08, FE-DOD-05, FE-SHELL-02.AC4 (PLAN a.4, TECH-SPECS §2.5, §5.2): the Create project modal.
// The form is local state of an inner component that exists only while the modal is open, so every way of closing it (Esc, X, Cancel,
// scrim) or leaving (Reset demo remounts the shell, a reload rebuilds everything) drops the form (FE-DEMO-08.AC1/AC2).
// Data only through ports (R7): people.directory, createProject.create. Production creates through POST /projects with the name
// only (A9). Owner decision 2026-10-15 (both trees): the form has the name and the contributors only; the source links, Test access
// and the Jira/Confluence switches are gone (sources are added on Mission Control).
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { PeoplePicker } from "@/components/ui/PeoplePicker";
import { TextField } from "@/components/ui/TextField";
import type { Person } from "@/lib/domain/people";
import { usePorts, useRoutes } from "@/lib/data/provider";
import { buildInput, createFailure, initialForm, nameError, type FormState } from "./form-logic";
import "./cp-gaps.css";
import { COPY } from "@/lib/copy";

const NAME_ID = "cp-name";

export function CreateProjectModal(props: Readonly<{ open: boolean; onClose: () => void }>) {
  if (!props.open) return null; // closing unmounts the form: the next opening starts from the defaults (FE-DEMO-08.AC1)
  return <CreateProjectForm onClose={props.onClose} />;
}

/** The data layer of the form: the ports and the route builder, read once. */
function useCreateProjectData() {
  return { ports: usePorts(), routes: useRoutes() };
}

function CreateProjectForm({ onClose: closeSlot }: Readonly<{ onClose: () => void }>) {
  const { ports, routes } = useCreateProjectData();
  const router = useRouter();
  const [form, setForm] = useState<FormState>(initialForm);
  const [nameMsg, setNameMsg] = useState<string | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // Set once the modal is closed by the user (Esc, X, Cancel, scrim) or unmounted: a create that completes afterwards still happens
  // (the port call is not cancellable) but the route is NOT opened (fix1 #8: both or neither; here "create, no navigation").
  const closed = useRef(false);
  const fields = useRef<HTMLDivElement>(null);
  const isClosed = () => closed.current;
  const focusName = () => {
    fields.current?.querySelector<HTMLInputElement>(`#${NAME_ID}`)?.focus();
  };
  useEffect(() => {
    closed.current = false;
    return () => {
      closed.current = true;
    };
  }, []);
  const onClose = () => {
    closed.current = true;
    closeSlot();
  };
  const patch = (p: Partial<FormState>) => {
    setForm((f) => ({ ...f, ...p }));
  };

  // FE-CRT-02: suggestions come from the people port (people only, FE-PPL-02).
  useEffect(() => {
    let live = true;
    ports.people.directory().then(
      (list) => {
        if (live) setPeople(list);
      },
      () => {
        if (live) setPeople([]);
      },
    );
    return () => {
      live = false;
    };
  }, [ports]);

  async function submit() {
    if (busy || isClosed()) return;
    const msg = nameError(form.name);
    setNameMsg(msg);
    const input = buildInput(form);
    if (msg !== null || input === null) {
      focusName();
      return;
    }
    setBusy(true);
    setFailed(false);
    try {
      const project = await ports.createProject.create(input);
      if (isClosed()) return; // closed while the create was pending: the project exists, the route stays
      closeSlot(); // the slot closes; the Modal returns the focus to "New"
      router.push(routes.missionControl(project.id)); // FE-CRT-06.AC2: Mission Control opens Empty (the shell then makes it current)
    } catch (e) {
      if (isClosed()) return;
      setBusy(false);
      const f = createFailure(e, form.name);
      setNameMsg(f.nameMsg);
      setFailed(f.alert);
      focusName();
    }
  }

  return (
    <Modal
      title="Create project"
      description={"Name the project and add its contributors. You connect its sources in Mission Control."}
      onClose={onClose}
      footer={
        <>
          <Button variant="glass" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              void submit();
            }}
            disabled={busy}
            data-testid="cp-create"
          >
            Create
          </Button>
        </>
      }
    >
      <div ref={fields} data-testid="create-project-modal" className="cp-fields">
        <TextField
          label="Name"
          id={NAME_ID}
          value={form.name}
          placeholder={COPY.projectName}
          error={nameMsg ?? undefined}
          onChange={(e) => {
            patch({ name: e.target.value });
            if (nameMsg !== null) setNameMsg(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <PeoplePicker
          label="Contributors"
          people={people}
          value={form.contributorIds}
          onChange={(ids) => {
            patch({ contributorIds: ids });
          }}
        />
        {failed ? (
          <p className="body" role="alert" data-testid="cp-error">
            The project could not be created. Try again.
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
