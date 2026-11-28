"use client";
// docs/projects/ui.md#general-tab: what the General tab and the V2 Members dialog both do, free of any look. It reads the members and the
// name, and turns every action into a port call through settings-logic.ts (rename, add, remove, cancel an invite, leave). The caller
// decides where "the project is gone" and "you left" lead (`onHome`).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useToast } from "@/components/shared/ui/Toast";
import type { MembersState } from "@/lib/domain/members";
import type { AddOutcome } from "./use-add-row";
import { useMembersPort, useProjectsPort } from "./use-settings-ports";
import {
  checkName,
  confirmOf,
  isGone,
  nameErrorText,
  runAdd,
  runConfirm,
  runRename,
  type Confirm,
  type Effects,
  type Row,
} from "./settings-logic";

export function useMembersController(projectId: string, projectName: string | null, onHome: () => void) {
  const members = useMembersPort();
  const projects = useProjectsPort();
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

  const load = useCallback(() => {
    members.list(projectId).then(
      (s) => {
        if (live.current) setState(s);
      },
      (e: unknown) => {
        if (!live.current) return;
        if (isGone(e))
          onHome(); // removed meanwhile: as for any inaccessible project
        else setState((prev) => (prev === null || prev === "failed" ? "failed" : prev));
      },
    );
  }, [members, projectId, onHome]);
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

  const fx = useMemo<Effects>(() => ({ toast, reload: load, left: onHome, gone: onHome }), [toast, load, onHome]);
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
  const type = (v: string) => {
    setDraft(v);
    setNameError(undefined);
  };
  const askRow = (row: Row) => {
    setConfirm(confirmOf(row));
  };
  const askLeave = () => {
    setConfirm({ kind: "leave" });
  };
  const dismiss = () => {
    setConfirm(null);
  };
  return {
    state,
    load,
    draft,
    type,
    nameError: nameError ?? nameErrorText(check),
    canSave: check.kind === "ok",
    saving,
    save,
    confirm,
    confirming,
    confirmNow,
    askRow,
    askLeave,
    dismiss,
    addKey,
    add,
  };
}
