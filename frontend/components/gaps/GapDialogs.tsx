"use client";
// FR-GWEB-04 (answers.md "Dismissing", "Revert", "Raising a gap by hand"): the dialogs of the Gaps screen, each a P04 Modal (labelled by its
// title, focus trapped, Esc closes, focus returns to the opener; a running write disables closing).
//  - DismissDialog: "Not a gap" with a reason ("Not a gap", "Not spec", "Duplicate", "Won't decide now", or free text 1..500). For "Not spec"
//    on a gap whose witness names its document and section, it offers to also add a `section` noise rule for that section.
//  - ReasonDialog: a reason 1..500 for withdrawing an answer (the card) or reverting an AI answer (the review list).
//  - RaiseGapDialog: a person's gap: the question (10..1000), the spec ids it is about (1..20), the kind (default missing), the severity
//    (optional); `400 answer_invalid` is shown at its field.
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { OTHER_VALUE, OptionCards } from "@/components/ui/OptionCards";
import { Select } from "@/components/ui/Select";
import { TextField } from "@/components/ui/TextField";
import { isDataError } from "@/lib/domain/errors";
import { GAP_KINDS, GAP_SEVERITIES, type GapKind, type GapSeverity, type GapsPort } from "@/lib/domain/gaps";
import { DISMISS_REASONS, KIND_LABEL, SEVERITY_LABEL, failureText } from "./gaps-logic";

const MAX_REASON = 500;

/** A reason within 1..500 characters (trimmed), or null. */
const reasonOf = (text: string): string | null => {
  const t = text.trim();
  return t.length === 0 || t.length > MAX_REASON ? null : t;
};

export function DismissDialog({
  section,
  onConfirm,
  onClose,
}: Readonly<{
  /** The witness section a "Not spec" dismissal may also mark as not spec, or null. */
  section: { value: string; label: string } | null;
  onConfirm: (reason: string, markSection: boolean) => Promise<void>;
  onClose: () => void;
}>) {
  const [picked, setPicked] = useState<string | undefined>(undefined);
  const [other, setOther] = useState("");
  const [mark, setMark] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reason = picked === OTHER_VALUE ? reasonOf(other) : (picked ?? null);
  const offerRule = picked === "not spec" && section !== null;
  const confirm = async () => {
    if (reason === null) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(reason, offerRule && mark);
    } catch (e) {
      setError(failureText(e));
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      size="md"
      title="Not a gap"
      description="Say why this is not a gap. The same question stays quiet until its lines change."
      onClose={onClose}
      closeDisabled={busy}
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void confirm()} disabled={reason === null || busy} data-testid="gap-dismiss-confirm">
            Dismiss gap
          </Button>
        </>
      }
    >
      <div className="gap-dialog" data-testid="gap-dismiss-dialog">
        <div data-testid="gap-dismiss-reasons">
          <OptionCards
            label="Reason"
            options={DISMISS_REASONS}
            allowOther
            otherLabel="Another reason"
            otherPlaceholder="Write the reason (up to 500 characters)"
            onOtherChange={setOther}
            value={picked}
            onChange={setPicked}
          />
        </div>
        {picked === OTHER_VALUE && other.trim().length > MAX_REASON ? (
          <p className="gap-alert body-sm" role="alert">
            Keep the reason to 500 characters.
          </p>
        ) : null}
        {offerRule ? (
          <div className="gap-dialog" data-testid="gap-dismiss-noise-rule">
            <p className="gap-text body-sm">
              Also mark the section <strong>{section.label}</strong> as not spec, so gap runs skip it?
            </p>
            <OptionCards
              type="yesno"
              label={`Also mark the section ${section.label} as not spec`}
              yesLabel="Yes, mark it"
              noLabel="No"
              value={mark ? "yes" : "no"}
              onChange={(v) => {
                setMark(v === "yes");
              }}
            />
          </div>
        ) : null}
        {error === null ? null : (
          <p className="gap-alert body-sm" role="alert" data-testid="gap-dismiss-error">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

export function ReasonDialog({
  title,
  description,
  confirmLabel,
  onConfirm,
  onClose,
}: Readonly<{
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: (reason: string) => Promise<void>;
  onClose: () => void;
}>) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reason = reasonOf(text);
  const confirm = async () => {
    if (reason === null) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(reason);
    } catch (e) {
      setError(failureText(e));
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      size="md"
      title={title}
      description={description}
      onClose={onClose}
      closeDisabled={busy}
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void confirm()} disabled={reason === null || busy} data-testid="revert-confirm">
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="gap-dialog" data-testid="revert-dialog">
        <TextField
          label="Reason"
          multiline
          rows={3}
          value={text}
          maxLength={MAX_REASON}
          help="Kept in the gap's history (up to 500 characters)."
          onChange={(e) => {
            setText(e.target.value);
          }}
          data-testid="revert-reason"
        />
        {error === null ? null : (
          <p className="gap-alert body-sm" role="alert" data-testid="revert-error">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

/** "REQ::A, REQ::B" or one per line -> the ids, each once. */
export function specIdsOf(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\s,;]+/)
        .map((s) => s.trim())
        .filter((s) => s !== ""),
    ),
  ];
}

const KIND_CHOICES = GAP_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] }));
const SEVERITY_CHOICES = [
  { value: "", label: "Let Specflow decide" },
  ...GAP_SEVERITIES.map((s) => ({ value: s, label: SEVERITY_LABEL[s] })),
];

export function RaiseGapDialog({
  gaps,
  projectId,
  onRaised,
  onClose,
}: Readonly<{ gaps: GapsPort; projectId: string; onRaised: (gapId: string, created: boolean) => void; onClose: () => void }>) {
  const [question, setQuestion] = useState("");
  const [about, setAbout] = useState("");
  const [kind, setKind] = useState<GapKind>("missing");
  const [severity, setSeverity] = useState<GapSeverity | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ field: string; text: string } | null>(null);
  const ids = specIdsOf(about);
  const q = question.trim();
  const questionOk = q.length >= 10 && q.length <= 1000;
  const aboutOk = ids.length >= 1 && ids.length <= 20;
  const submit = async () => {
    if (!questionOk || !aboutOk) {
      setError(
        questionOk
          ? { field: "about", text: "Name 1 to 20 spec ids, separated by commas or new lines." }
          : { field: "question", text: "The question must be 10 to 1,000 characters." },
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await gaps.raise(projectId, { question: q, kind, about: ids, ...(severity === "" ? {} : { severity }) });
      onRaised(r.gapId, r.created);
    } catch (e) {
      const field = isDataError(e) && e.detail?.apiCode === "answer_invalid" ? (e.detail.field ?? "") : "";
      setError({ field, text: failureText(e) });
      setBusy(false);
    }
  };
  const fieldError = (f: string) => (error?.field === f ? error.text : undefined);
  return (
    <Modal
      open
      size="md"
      title="Raise a gap"
      description="Ask a question the specification leaves open. Specflow investigates it and adds options to answer."
      onClose={onClose}
      closeDisabled={busy}
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} disabled={busy} data-testid="raise-submit">
            Raise gap
          </Button>
        </>
      }
    >
      <div className="gap-dialog" data-testid="raise-gap-dialog">
        <TextField
          label="Question"
          multiline
          rows={3}
          value={question}
          maxLength={1000}
          help="10 to 1,000 characters."
          error={fieldError("question")}
          onChange={(e) => {
            setQuestion(e.target.value);
          }}
          data-testid="raise-question"
        />
        <TextField
          label="Spec ids"
          value={about}
          help="The requirements or features it is about: 1 to 20 ids, separated by commas."
          error={fieldError("about") ?? fieldError("quotes")}
          onChange={(e) => {
            setAbout(e.target.value);
          }}
          data-testid="raise-about"
        />
        <div className="gap-dialog__row">
          <Select
            label="Kind of gap"
            options={KIND_CHOICES}
            value={kind}
            onChange={(v) => {
              setKind(GAP_KINDS.find((k) => k === v) ?? "missing");
            }}
          />
          <Select
            label="Severity (optional)"
            options={SEVERITY_CHOICES}
            value={severity}
            onChange={(v) => {
              setSeverity(GAP_SEVERITIES.find((s) => s === v) ?? "");
            }}
          />
        </div>
        {error !== null && error.field !== "question" && error.field !== "about" && error.field !== "quotes" ? (
          <p className="gap-alert body-sm" role="alert" data-testid="raise-error">
            {error.text}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
