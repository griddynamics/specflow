"use client";
// docs/projects/ui.md#general-tab (layout A2): the General tab. The admin sees the name with Save, the add row with suggestions and the
// list with x; a collaborator sees the read-only list and "Leave project" (D29: no name block). What the screen shows comes from
// `MembersState.role` and `is_you`, never from `/me` or the project's creator. The rules and the port calls are in settings-logic.ts.
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useEmailSuggestions } from "@/components/members/use-email-suggestions";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import type { MembersState, Suggestion } from "@/lib/domain/members";
import { useMembersPort, useProjectsPort, useSettingsRoutes } from "./use-settings-ports";
import { AddRowView, GeneralView, LoadFailed } from "./parts";
import { checkName, confirmOf, isGone, nameErrorText, runAdd, runConfirm, runRename, type Confirm, type Effects } from "./settings-logic";

type AddOutcome = Awaited<ReturnType<typeof runAdd>>;

function AddRow({ projectId, onAdd }: Readonly<{ projectId: string; onAdd: (text: string) => Promise<AddOutcome> }>) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | undefined>(undefined);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [busy, setBusy] = useState(false);
  const { items } = useEmailSuggestions(text, projectId);

  const add = () => {
    if (busy) return;
    const pending = onAdd(text);
    setBusy(true);
    setOpen(false);
    void pending.then((result) => {
      setBusy(false);
      setError(result.ok ? undefined : (result.error ?? undefined)); // a success remounts the row (new key), which empties the field
    });
  };
  const pick = (s: Suggestion) => {
    setText(s.email);
    setError(undefined);
    setOpen(false);
  };
  return (
    <AddRowView
      text={text}
      error={error}
      items={items}
      open={open}
      active={active}
      busy={busy}
      onText={(v) => {
        setText(v);
        setError(undefined);
      }}
      onActive={setActive}
      onOpen={setOpen}
      onPick={pick}
      onAdd={add}
    />
  );
}

export function GeneralTab({ projectId, projectName }: Readonly<{ projectId: string; projectName: string | null }>) {
  const members = useMembersPort();
  const projects = useProjectsPort();
  const routes = useSettingsRoutes();
  const router = useRouter();
  const toast = useToast();
  const [state, setState] = useState<MembersState | "failed" | null>(null);
  const [saved, setSaved] = useState(projectName ?? "");
  const [draft, setDraft] = useState(projectName ?? "");
  const [nameError, setNameError] = useState<string | undefined>(undefined);
  const [saving, setSaving] = useState(false);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [addKey, setAddKey] = useState(0);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const goHome = useCallback(() => {
    router.replace(routes.home()); // `/` reads the project list again (the shell too) and opens the newest remaining project
  }, [router, routes]);

  const load = useCallback(() => {
    members.list(projectId).then(
      (s) => {
        if (live.current) setState(s);
      },
      (e: unknown) => {
        if (!live.current) return;
        if (isGone(e))
          goHome(); // removed meanwhile: as for any inaccessible project
        else setState((prev) => (prev === null || prev === "failed" ? "failed" : prev));
      },
    );
  }, [members, projectId, goHome]);
  useEffect(() => {
    load();
  }, [load]);

  // The page could not read the name on the server: ask the port once.
  useEffect(() => {
    if (projectName !== null) return;
    projects.get(projectId).then(
      (p) => {
        if (!live.current) return;
        setSaved(p.name);
        setDraft(p.name);
      },
      () => undefined, // the field stays empty; Save stays off until a name is typed
    );
  }, [projects, projectId, projectName]);

  const fx = useMemo<Effects>(() => ({ toast, reload: load, left: goHome, gone: goHome }), [toast, load, goHome]);

  if (state === null) {
    return (
      <div aria-hidden data-testid="settings-loading">
        <Skeleton variant="block" height={200} />
      </div>
    );
  }
  if (state === "failed") return <LoadFailed onRetry={load} />;

  const check = checkName(draft, saved);

  const save = () => {
    if (check.kind !== "ok" || saving) return;
    setSaving(true);
    void runRename(check.name, projectId, projects, fx).then((r) => {
      if (!live.current) return;
      setSaving(false);
      if (r.ok) {
        setSaved(r.name);
        setDraft(r.name);
        setNameError(undefined);
      } else setNameError(r.error ?? undefined);
    });
  };
  const add = async (text: string): Promise<AddOutcome> => {
    const r = await runAdd(text, projectId, members, fx);
    if (r.ok) setAddKey((k) => k + 1);
    return r;
  };
  const confirmNow = () => {
    if (confirm === null || confirming) return;
    setConfirming(true);
    void runConfirm(confirm, projectId, members, fx).then(() => {
      if (!live.current) return;
      setConfirming(false);
      setConfirm(null);
    });
  };

  return (
    <GeneralView
      state={state}
      draft={draft}
      nameError={nameError ?? nameErrorText(check)}
      canSave={check.kind === "ok"}
      saving={saving}
      confirm={confirm}
      confirming={confirming}
      addSlot={<AddRow key={addKey} projectId={projectId} onAdd={add} />}
      onDraft={(v) => {
        setDraft(v);
        setNameError(undefined);
      }}
      onSave={save}
      onRow={(row) => {
        setConfirm(confirmOf(row));
      }}
      onLeave={() => {
        setConfirm({ kind: "leave" });
      }}
      onConfirm={confirmNow}
      onDismiss={() => {
        setConfirm(null);
      }}
    />
  );
}
