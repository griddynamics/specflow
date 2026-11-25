"use client";
// The Google Drive connect flow shared by the V1 modal and the V2 canvas form (FR-GDUI-0001..0020): the account email, the pasted link, the
// connection check, the save, and the cutoffs the UI owns. It makes no layout decision: a view shows what this returns. The pasted link goes
// only to `SourcesPort` (never fetched here, NFR-SEC-0001).
import { useEffect, useRef, useState } from "react";
import { truthy } from "@/components/ui/utils";
import { parseDriveLink } from "@/lib/domain/logic/source-link";
import type { DriveIdentity, DriveReason, DriveTestResult, SourcesPort } from "@/lib/domain/sources";
import { driveReasonMessage, driveSummary, SAVE_DEADLINE_MS, settle, TEST_DEADLINE_MS } from "./drive-logic";

type Tested = Extract<DriveTestResult, { ok: true }>;
type Outcome = { kind: "tested"; result: Tested } | { kind: "failed"; reason: DriveReason } | null;
export type DrivePhase = "idle" | "testing" | "saving";

export function useDriveFlow({ projectId, sources, onSaved }: Readonly<{ projectId: string; sources: SourcesPort; onSaved: () => void }>) {
  const [identity, setIdentity] = useState<DriveIdentity | null>(null);
  const [link, setLink] = useState("");
  const [badLink, setBadLink] = useState(false);
  const [phase, setPhase] = useState<DrivePhase>("idle");
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [saveNote, setSaveNote] = useState(false);
  const run = useRef(0); // a later answer of an older run (or after closing) is ignored
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    sources.driveIdentity().then(
      (i) => {
        if (alive.current) setIdentity(i);
      },
      () => {
        if (alive.current) setIdentity({ ok: false, reason: "unknown_error" });
      },
    );
    return () => {
      alive.current = false;
      run.current += 1;
    };
  }, [sources]);

  const email = truthy(identity?.ok) ? identity.email : null;
  const busy = phase !== "idle";

  async function test() {
    if (busy || !truthy(email) || !link.trim()) return;
    if (!truthy(parseDriveLink(link))) {
      setBadLink(true); // FR-GDUI-0008: no request leaves for a link that is not accepted (NFR-SEC-0001)
      return;
    }
    const mine = ++run.current;
    setPhase("testing");
    setOutcome(null);
    setSaveNote(false);
    const r = await settle(sources.testDrive(projectId, link), TEST_DEADLINE_MS);
    if (!alive.current || mine !== run.current) return;
    setPhase("idle");
    if (r.kind === "timeout") setOutcome({ kind: "failed", reason: "too_slow" });
    else if (r.kind === "lost") setOutcome({ kind: "failed", reason: "unknown_error" });
    else setOutcome(r.value.ok ? { kind: "tested", result: r.value } : { kind: "failed", reason: r.value.reason });
  }

  async function save(exclude?: readonly string[]) {
    if (busy || outcome?.kind !== "tested") return;
    const mine = ++run.current;
    setPhase("saving");
    setSaveNote(false);
    const r = await settle(sources.saveDrive(projectId, link, exclude), SAVE_DEADLINE_MS);
    if (!alive.current || mine !== run.current) return;
    if (r.kind === "ok" && r.value.ok) {
      onSaved(); // FR-SRC-0024: the modal closes and the screen shows the new card
      return;
    }
    setPhase("idle");
    if (r.kind === "timeout")
      setSaveNote(true); // NFR-REL-0002: the source may exist; closing is available again
    else setOutcome({ kind: "failed", reason: r.kind === "ok" && !r.value.ok ? r.value.reason : "unknown_error" }); // FR-GDUI-0019: result removed, back to Test connection
  }

  const summary = outcome?.kind === "tested" ? driveSummary(outcome.result) : null;
  const canSave = summary !== null && !summary.nothingToImport;
  const blocked = canSave ? busy : busy || !link.trim() || !truthy(email);
  return {
    identity,
    email,
    link,
    badLink,
    phase,
    busy,
    saving: phase === "saving",
    saveNote,
    summary,
    /** The files of the folder, once it is tested (the V2 form lists them). */
    tested: outcome?.kind === "tested" ? outcome.result : null,
    canSave,
    blocked,
    failure: outcome?.kind === "failed" ? driveReasonMessage(outcome.reason, email) : null,
    /** FR-GDUI-0015 and FR-GDUI-0008.AC3: any edit removes the message and the check result. */
    edit: (value: string) => {
      setLink(value);
      setBadLink(false);
      setOutcome(null);
      setSaveNote(false);
    },
    /** FR-GDUI-0008: a link that is not a Drive folder link is said so when the field is left. */
    leave: () => {
      if (link.trim() && !truthy(parseDriveLink(link))) setBadLink(true);
    },
    /** The primary action: "Save" once the check passed (without the files in `exclude`), else "Test connection". */
    act: (exclude?: readonly string[]) => (canSave ? save(exclude) : test()),
  };
}
