"use client";
// FR-GWEB-04 (gap answers.md "The question card", "Answering", "Revert", "Dismissing"; api.md "User interface"): the question card of one
// gap in the P04 Modal (focus trap, Esc, focus returns to the opener). Chips (severity, kind, origin, status, blocking), the assignee, the
// title, the question with its options, why, the ABOUT and IMPACTS nodes, the quoted witness lines with their source (document › section ·
// lines when the read names them), the reach (stories, tasks), the recorded answer, and the collapsible history with its follow-ups. With a
// `control` (the Gaps screen) an open or answered gap is answerable (GapAnswer.tsx: options as a radio group, "Other", the edits preview,
// the placement, Submit in the footer, stale handling) and can be dismissed ("Not a gap", GapDialogs.tsx); a recorded answer that is
// pending, applied or absorbed can be withdrawn ("Withdraw answer", a revert with a reason). A gap that is not resolved can be assigned to
// a member or unassigned ("Assigned to", FR-GWEB-06; answers.md "Who answers"). Without a `control` the card is read-only.
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { QuestionBlock } from "@/components/ui/QuestionBlock";
import { Select } from "@/components/ui/Select";
import { SeverityBadge } from "@/components/ui/SeverityBadge";
import { Tag } from "@/components/ui/Tag";
import type { Gap, GapNode, RevertResult } from "@/lib/domain/gaps";
import { AnswerForm, AnswerMessages, useGapAnswer, type CardControl, type GapAnswerState } from "./GapAnswer";
import { DismissDialog, ReasonDialog } from "./GapDialogs";
import { GapHistory } from "./GapHistory";
import {
  ASSIGNED_TO_NOBODY,
  CLARIFICATION_STATUS_LABEL,
  KIND_LABEL,
  ORIGIN_LABEL,
  SEVERITY_LABEL,
  SEVERITY_LEVEL,
  STATUS_LABEL,
  answerable,
  appliedWhenText,
  assigneeChoices,
  assigneeText,
  clarificationText,
  confidenceText,
  failureText,
  memberNames,
  quoteSource,
  withdrawable,
  witnessSection,
} from "./gaps-logic";
import "./gaps.css";

export type { CardControl } from "./GapAnswer";

function NodeChips({ title, nodes, testId }: Readonly<{ title: string; nodes: GapNode[]; testId: string }>) {
  if (nodes.length === 0) return null;
  return (
    <div className="gap-nodes">
      <h4 className="gap-nodes__rel eyebrow">{title}</h4>
      <ul className="gap-nodes__list" aria-label={title} data-testid={testId}>
        {nodes.map((n) => (
          <li key={n.specId} title={n.label === "" ? n.name : `${n.label}: ${n.name}`}>
            <Tag>
              <span>{n.specId}</span>
              {n.name === "" ? null : <span className="gap-node__name">{n.name}</span>}
            </Tag>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The options as decision option cards, read-only; the recorded option is shown chosen. */
function GapQuestion({ gap }: Readonly<{ gap: Gap }>) {
  const value = (label: string | null) => gap.options.find((o) => o.label === label)?.rank;
  const chosen = gap.clarification === null ? undefined : value(gap.clarification.optionLabel);
  const recommended = gap.options.find((o) => o.recommended)?.rank;
  return (
    <section className="gap-sec" aria-label="Question" data-testid="gap-question">
      <h3 className="gap-sec__title eyebrow">Question</h3>
      <QuestionBlock
        number="Q"
        question={gap.question}
        type="choice"
        readOnly
        options={gap.options.map((o) => ({ value: String(o.rank), label: o.label, description: o.detail === "" ? undefined : o.detail }))}
        defaultValue={chosen === undefined ? undefined : String(chosen)}
        recommended={recommended === undefined ? undefined : String(recommended)}
      />
      {gap.options.length === 0 ? (
        <div data-testid="gap-no-options">
          <EmptyState title="Options are being prepared" icon="clock">
            The gap run is still working out the answers to offer for this question.
          </EmptyState>
        </div>
      ) : null}
    </section>
  );
}

/** The recorded answer: its words, who answered, its status, an AI answer's confidence and basis, and "Withdraw answer". */
function RecordedAnswer({ gap, onWithdraw }: Readonly<{ gap: Gap; onWithdraw: (() => void) | null }>) {
  const c = gap.clarification;
  if (c === null) return null;
  const confidence = confidenceText(c.confidence);
  return (
    <section className="gap-sec" aria-label="Answer" data-testid="gap-answer">
      <h3 className="gap-sec__title eyebrow">Answer</h3>
      <p className="gap-text">{clarificationText(c)}</p>
      <p className="gap-quote__src body-sm">
        {c.mode === "ai" ? "Answered by AI" : `Answered by ${c.answeredBy}`} · {CLARIFICATION_STATUS_LABEL[c.status]}
        {confidence === null ? null : ` · ${confidence}`}
        {c.appliedBuildId === null || c.appliedBuildId === undefined ? null : ` · applied by build ${c.appliedBuildId}`}
      </p>
      {(c.basis ?? []).length === 0 ? null : (
        <ul className="gap-quotes" aria-label="Basis">
          {(c.basis ?? []).map((b, i) => (
            <li key={`${b.fragmentId}-${String(i)}`}>
              <blockquote className="gap-quote">{b.quote}</blockquote>
            </li>
          ))}
        </ul>
      )}
      {onWithdraw === null ? null : (
        <div>
          <Button variant="glass" size="sm" onClick={onWithdraw} data-testid="gap-withdraw">
            Withdraw answer
          </Button>
        </div>
      )}
    </section>
  );
}

function ReadOnlyParts({ gap }: Readonly<{ gap: Gap }>) {
  const reach = gap.reach;
  return (
    <>
      {gap.why === "" ? null : (
        <section className="gap-sec" aria-label="Why" data-testid="gap-why">
          <h3 className="gap-sec__title eyebrow">Why</h3>
          <p className="gap-text">{gap.why}</p>
        </section>
      )}
      {gap.about.length + gap.impacts.length === 0 ? null : (
        <section className="gap-sec" aria-label="Affected nodes">
          <h3 className="gap-sec__title eyebrow">Affected nodes</h3>
          <NodeChips title="About" nodes={gap.about} testId="gap-about" />
          <NodeChips title="Impacts" nodes={gap.impacts} testId="gap-impacts" />
        </section>
      )}
      {gap.witness.length === 0 ? null : (
        <section className="gap-sec" aria-label="Quotes">
          <h3 className="gap-sec__title eyebrow">Quotes</h3>
          <ul className="gap-quotes" data-testid="gap-quotes">
            {gap.witness.map((q, i) => (
              <li key={`${q.fragmentId}-${String(i)}`}>
                <blockquote className="gap-quote">{q.quote}</blockquote>
                <span className="gap-quote__src body-sm">{quoteSource(q)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {reach === null || reach.stories.length + reach.tasks.length === 0 ? null : (
        <section className="gap-sec" aria-label="Reach" data-testid="gap-reach">
          <h3 className="gap-sec__title eyebrow">Reach</h3>
          <p className="gap-text body-sm">
            {reach.stories.length} {reach.stories.length === 1 ? "story" : "stories"}, {reach.tasks.length}{" "}
            {reach.tasks.length === 1 ? "task" : "tasks"}
          </p>
          <ul className="gap-nodes__list" aria-label="Stories and tasks">
            {[...reach.stories, ...reach.tasks].map((id) => (
              <li key={id}>
                <Tag>{id}</Tag>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

/** "Assigned to": a select of Unassigned and the members while the gap is not resolved (any member assigns; the card reads the gap again
 * and the lists read again), else the assignee in words. */
function Assignee({ gap, control }: Readonly<{ gap: Gap; control?: CardControl }>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (control === undefined || gap.status === "resolved") {
    if (gap.assignedTo === null) return null;
    return (
      <p className="gap-assignee gap-assignee__msg body-sm" data-testid="gap-assignee">
        Assigned to {assigneeText(gap.assignedTo, control?.members ?? [])}
      </p>
    );
  }
  const change = async (value: string) => {
    const userId = value === ASSIGNED_TO_NOBODY ? null : value;
    if (busy || userId === gap.assignedTo) return;
    setBusy(true);
    setError(null);
    try {
      await control.gaps.assign(control.projectId, gap.id, userId);
      control.onChanged();
      await control.onReload();
    } catch (e) {
      setError(`The assignment was not saved. ${failureText(e)}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="gap-assignee" data-testid="gap-assignee" aria-busy={busy}>
      <Select
        label="Assigned to"
        options={assigneeChoices(control.members, gap.assignedTo)}
        value={gap.assignedTo ?? ASSIGNED_TO_NOBODY}
        onChange={(v) => void change(v)}
      />
      {error === null ? null : (
        <p className="gap-assignee__msg body-sm" role="alert" data-testid="gap-assign-error">
          {error}
        </p>
      )}
    </div>
  );
}

function revertedText(r: RevertResult): string {
  return r.followUp
    ? "Answer withdrawn. A follow-up gap is open for a person to answer; it is linked in the history."
    : "Answer withdrawn. The gap is open again.";
}

/** Close, and for an answerable gap "Not a gap" and Submit (the one primary action of the card). */
function CardFooter({
  gap,
  a,
  editable,
  closeCard,
  askDismissal,
}: Readonly<{ gap: Gap; a: GapAnswerState; editable: boolean; closeCard: () => void; askDismissal: () => void }>) {
  return (
    <>
      <Button variant="glass" onClick={closeCard}>
        Close
      </Button>
      {editable ? (
        <>
          <Button variant="glass" onClick={askDismissal} data-testid="gap-dismiss">
            Not a gap
          </Button>
          <Button variant="primary" onClick={() => void a.submit()} disabled={!a.canSubmit} data-testid="gap-submit">
            {gap.status === "answered" ? "Change answer" : "Submit answer"}
          </Button>
        </>
      ) : null}
    </>
  );
}

/** The card's dialogs: "Not a gap" (with the "not spec" section rule) and "Withdraw answer". */
function CardDialog({
  gap,
  control,
  dialog,
  finish,
  onWithdrawn,
}: Readonly<{
  gap: Gap;
  control: CardControl;
  dialog: "dismiss" | "withdraw";
  finish: () => void;
  onWithdrawn: (r: RevertResult) => void;
}>) {
  const clrId = gap.clarification?.clrId;
  if (dialog === "dismiss") {
    const section = witnessSection(gap);
    return (
      <DismissDialog
        section={section}
        onClose={finish}
        onConfirm={async (reason, markSection) => {
          await control.gaps.dismiss(control.projectId, gap.id, reason);
          if (markSection && section !== null) {
            // The dismissal stands even when the rule is refused; the noise report offers the rule again.
            await control.gaps.addNoiseRule(control.projectId, { scope: "section", value: section.value, reason }).catch(() => undefined);
          }
          finish();
          control.onDismissed();
        }}
      />
    );
  }
  if (clrId === undefined) return null;
  return (
    <ReasonDialog
      title="Withdraw answer"
      description="The answer's text leaves the specification with the next build, and the question goes back to a person."
      confirmLabel="Withdraw answer"
      onClose={finish}
      onConfirm={async (reason) => {
        const r = await control.gaps.revert(control.projectId, clrId, reason);
        finish();
        onWithdrawn(r);
        control.onChanged();
        await control.onReload();
      }}
    />
  );
}

export function GapCard({ gap, onClose, control }: Readonly<{ gap: Gap; onClose: () => void; control?: CardControl }>) {
  const a = useGapAnswer(gap, control);
  const [dialog, setDialog] = useState<"dismiss" | "withdraw" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const editable = control !== undefined && answerable(gap);
  const canWithdraw = control !== undefined && withdrawable(gap);
  const appliedText = note ?? (a.result === null ? null : appliedWhenText(a.result));
  const closeDialog = () => {
    setDialog(null);
  };
  return (
    <Modal
      open
      size="lg"
      eyebrow={`Gap · ${KIND_LABEL[gap.kind]}`}
      title={gap.title}
      onClose={onClose}
      footer={
        <CardFooter
          gap={gap}
          a={a}
          editable={editable}
          closeCard={onClose}
          askDismissal={() => {
            setDialog("dismiss");
          }}
        />
      }
    >
      <div className="gap-meta" data-testid="gap-meta">
        <SeverityBadge level={SEVERITY_LEVEL[gap.severity]} label={SEVERITY_LABEL[gap.severity]} />
        <Tag>{KIND_LABEL[gap.kind]}</Tag>
        <Tag>{ORIGIN_LABEL[gap.origin]}</Tag>
        {gap.status === "open" ? null : (
          <span data-testid="gap-status">
            <Tag tone="success">{STATUS_LABEL[gap.status]}</Tag>
          </span>
        )}
        {gap.blocking ? <Tag tone="warning">Holds tasks</Tag> : null}
      </div>
      <Assignee gap={gap} control={control} />
      {editable ? (
        <section className="gap-sec" aria-label="Question" data-testid="gap-question">
          <h3 className="gap-sec__title eyebrow">Question</h3>
          <AnswerForm gap={gap} a={a} />
        </section>
      ) : (
        <GapQuestion gap={gap} />
      )}
      <AnswerMessages a={a} appliedText={appliedText} />
      <RecordedAnswer
        gap={gap}
        onWithdraw={
          canWithdraw
            ? () => {
                setDialog("withdraw");
              }
            : null
        }
      />
      <ReadOnlyParts gap={gap} />
      {gap.history === null || gap.history === undefined ? null : (
        <GapHistory
          history={gap.history}
          names={memberNames(control?.members ?? [])}
          href={control?.href ?? ((id) => `?gap=${encodeURIComponent(id)}`)}
          onOpenGap={control?.onOpenGap ?? (() => undefined)}
        />
      )}
      {dialog === null || control === undefined ? null : (
        <CardDialog
          gap={gap}
          control={control}
          dialog={dialog}
          finish={closeDialog}
          onWithdrawn={(r) => {
            setNote(revertedText(r));
          }}
        />
      )}
    </Modal>
  );
}
