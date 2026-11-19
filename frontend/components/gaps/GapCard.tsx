"use client";
// FR-GWEB-04 (gap answers.md "The question card", "Answering", "Applying answers", "Revert", "Dismissing"; api.md "User interface"): the
// question card of one gap in the P04 Modal (focus trap, Esc, focus returns to the opener). Chips ("New" for a gap the latest gap run
// found, severity, kind, origin, status: Staged with "Resolving…" while its rebuild runs, or Resolved; blocking), the title, the question
// with its options, why, the ABOUT and IMPACTS nodes, the quoted witness lines with their source (document › section · lines when the
// read names them), the reach (stories, tasks), the recorded answer (who staged it), and the collapsible history with its follow-ups. With a `control` (the Gaps screen) an open or
// staged gap is answerable (GapAnswer.tsx: options as a radio group, "Other", the edits preview, the placement, stale handling).
// The footer's primary button stages the answer ("Stage answer"; "Change answer" replaces a staged one); a gap a coding agent waits
// on is applied at once ("Answer and resolve", with the line that says so). An open gap can be dismissed ("Not a gap", GapDialogs.tsx);
// a staged answer can be unstaged ("Unstage": the gap is open again) and an applied or absorbed one withdrawn ("Withdraw answer"), both
// reverts with a reason. A gap whose rebuild runs (`applying`) is read-only. The assignee is changed in the inbox row (AssigneeMenu.tsx,
// FR-GWEB-06), not here. Without a `control` the card is read-only.
import { useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { QuestionBlock } from "@/components/ui/QuestionBlock";
import { SeverityBadge } from "@/components/ui/SeverityBadge";
import { Tag } from "@/components/ui/Tag";
import type { Gap, GapNode, RevertResult } from "@/lib/domain/gaps";
import { AnswerForm, AnswerMessages, useGapAnswer, type CardControl, type GapAnswerState } from "./GapAnswer";
import { DismissDialog, ReasonDialog } from "./GapDialogs";
import { GapHistory } from "./GapHistory";
import {
  AT_ONCE_TEXT,
  KIND_LABEL,
  NEW_LABEL,
  ORIGIN_LABEL,
  RESOLVING_LABEL,
  SEVERITY_LABEL,
  SEVERITY_LEVEL,
  STATUS_LABEL,
  answerMetaText,
  answerOutcomeText,
  answerable,
  clarificationText,
  holdsAgent,
  memberNames,
  quoteSource,
  submitLabel,
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

/** The recorded answer: its words, who staged or answered it and its status, an AI answer's confidence and basis. */
function RecordedAnswer({ gap, names }: Readonly<{ gap: Gap; names: Readonly<Record<string, string>> }>) {
  const c = gap.clarification;
  if (c === null) return null;
  const title = c.status === "pending" ? "Staged answer" : "Answer";
  return (
    <section className="gap-sec" aria-label={title} data-testid="gap-answer">
      <h3 className="gap-sec__title eyebrow">{title}</h3>
      <p className="gap-text">{clarificationText(c)}</p>
      <p className="gap-quote__src body-sm" data-testid="gap-answer-meta">
        {answerMetaText(gap, c, names)}
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

function revertedText(r: RevertResult, staged: boolean): string {
  if (staged) return "Answer unstaged. The gap is open again.";
  return r.followUp
    ? "Answer withdrawn. A follow-up gap is open for a person to answer; it is linked in the history."
    : "Answer withdrawn. The gap is open again.";
}

/** Close; "Not a gap" for an open gap; "Unstage" a staged answer or "Withdraw answer" an applied one; the answer button (the one
 * primary action of the card) for an answerable gap. */
function CardFooter({
  gap,
  a,
  editable,
  dismissable,
  withdraw,
  atOnceId,
  closeCard,
  askDismissal,
}: Readonly<{
  gap: Gap;
  a: GapAnswerState;
  editable: boolean;
  dismissable: boolean;
  withdraw: (() => void) | null;
  /** The id of the line that says the answer applies at once, when the card shows it. */
  atOnceId: string | undefined;
  closeCard: () => void;
  askDismissal: () => void;
}>) {
  return (
    <>
      <Button variant="glass" onClick={closeCard}>
        Close
      </Button>
      {dismissable ? (
        <Button variant="glass" onClick={askDismissal} data-testid="gap-dismiss">
          Not a gap
        </Button>
      ) : null}
      {withdraw === null ? null : (
        <Button variant="glass" onClick={withdraw} data-testid="gap-withdraw">
          {gap.clarification?.status === "pending" ? "Unstage" : "Withdraw answer"}
        </Button>
      )}
      {editable ? (
        <Button
          variant="primary"
          onClick={() => void a.submit()}
          disabled={!a.canSubmit}
          aria-describedby={atOnceId}
          data-testid="gap-submit"
        >
          {submitLabel(gap)}
        </Button>
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
  onWithdrawn: (note: string) => void;
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
  const staged = gap.clarification?.status === "pending";
  return (
    <ReasonDialog
      title={staged ? "Unstage answer" : "Withdraw answer"}
      description={
        staged
          ? "The staged answer is dropped before it reaches the spec, and the gap is open again."
          : "The answer's text leaves the specification with the next build, and the question goes back to a person."
      }
      confirmLabel={staged ? "Unstage" : "Withdraw answer"}
      onClose={finish}
      onConfirm={async (reason) => {
        const r = await control.gaps.revert(control.projectId, clrId, reason);
        finish();
        onWithdrawn(revertedText(r, staged));
        control.onChanged();
        await control.onReload();
      }}
    />
  );
}

/** The chips: "New", severity, kind, origin, the status (Staged, with "Resolving…" while its rebuild runs, or Resolved), "Holds tasks". */
function CardMeta({ gap, isNew }: Readonly<{ gap: Gap; isNew: boolean }>) {
  return (
    <div className="gap-meta" data-testid="gap-meta">
      {isNew ? (
        <span data-testid="gap-new">
          <Tag tone="info">{NEW_LABEL}</Tag>
        </span>
      ) : null}
      <SeverityBadge level={SEVERITY_LEVEL[gap.severity]} label={SEVERITY_LABEL[gap.severity]} />
      <Tag>{KIND_LABEL[gap.kind]}</Tag>
      <Tag>{ORIGIN_LABEL[gap.origin]}</Tag>
      {gap.status === "open" ? null : (
        <span data-testid="gap-status">
          <Tag tone={gap.status === "resolved" ? "success" : "info"}>{STATUS_LABEL[gap.status]}</Tag>
        </span>
      )}
      {gap.status === "applying" ? (
        <span data-testid="gap-resolving">
          <Tag tone="pending">{RESOLVING_LABEL}</Tag>
        </span>
      ) : null}
      {gap.blocking ? <Tag tone="warning">Holds tasks</Tag> : null}
    </div>
  );
}

/** The question: the answer form (and the line that says a coding agent's answer applies at once) or, read-only, the options. */
function CardQuestion({ gap, a, editable, atOnceId }: Readonly<{ gap: Gap; a: GapAnswerState; editable: boolean; atOnceId?: string }>) {
  if (!editable) return <GapQuestion gap={gap} />;
  return (
    <section className="gap-sec" aria-label="Question" data-testid="gap-question">
      <h3 className="gap-sec__title eyebrow">Question</h3>
      <AnswerForm gap={gap} a={a} />
      {atOnceId === undefined ? null : (
        <p className="gap-text body-sm" id={atOnceId} data-testid="gap-at-once">
          {AT_ONCE_TEXT}
        </p>
      )}
    </section>
  );
}

export function GapCard({
  gap,
  isNew = false,
  onClose,
  control,
}: Readonly<{ gap: Gap; isNew?: boolean; onClose: () => void; control?: CardControl }>) {
  const a = useGapAnswer(gap, control);
  const [dialog, setDialog] = useState<"dismiss" | "withdraw" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const editable = control !== undefined && answerable(gap);
  const canWithdraw = control !== undefined && withdrawable(gap);
  const outcome = note ?? (a.result === null ? null : answerOutcomeText(a.result));
  const names = memberNames(control?.members ?? []);
  const atOnceId = useId();
  const describedBy = editable && holdsAgent(gap) ? atOnceId : undefined;
  const withdraw = canWithdraw
    ? () => {
        setDialog("withdraw");
      }
    : null;
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
          dismissable={control !== undefined && gap.status === "open"}
          atOnceId={describedBy}
          withdraw={withdraw}
          closeCard={onClose}
          askDismissal={() => {
            setDialog("dismiss");
          }}
        />
      }
    >
      <CardMeta gap={gap} isNew={isNew} />
      <CardQuestion gap={gap} a={a} editable={editable} atOnceId={describedBy} />
      <AnswerMessages a={a} outcome={outcome} />
      <RecordedAnswer gap={gap} names={names} />
      <ReadOnlyParts gap={gap} />
      {gap.history === null || gap.history === undefined ? null : (
        <GapHistory
          history={gap.history}
          names={names}
          href={control?.href ?? ((id) => `?gap=${encodeURIComponent(id)}`)}
          onOpenGap={control?.onOpenGap ?? (() => undefined)}
        />
      )}
      {dialog === null || control === undefined ? null : (
        <CardDialog gap={gap} control={control} dialog={dialog} finish={closeDialog} onWithdrawn={setNote} />
      )}
    </Modal>
  );
}
