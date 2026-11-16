"use client";
// FR-GWEB-04 (gap answers.md "The question card"; api.md "User interface"), read-only: the question card of one gap in the P04 Modal (focus
// trap, Esc, focus returns to the opener). Chips (severity, kind, origin, status, blocking), the title, the question with its options in the
// decision QuestionBlock (read-only; the recommended option carries the "Recommended" mark), why, the ABOUT and IMPACTS nodes, the quoted
// witness lines with their source, the reach (stories, tasks) and, once answered, the recorded answer. No answering control: answering,
// "Other", the edits preview, placement and "Not a gap" come with the write routes.
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { QuestionBlock } from "@/components/ui/QuestionBlock";
import { SeverityBadge } from "@/components/ui/SeverityBadge";
import { Tag } from "@/components/ui/Tag";
import type { Gap, GapNode } from "@/lib/domain/gaps";
import { KIND_LABEL, ORIGIN_LABEL, SEVERITY_LABEL, SEVERITY_LEVEL, STATUS_LABEL, clarificationText, quoteSource } from "./gaps-logic";
import "./gaps.css";

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

/** The options as decision option cards; the recorded option is shown chosen. */
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

export function GapCard({ gap, onClose }: Readonly<{ gap: Gap; onClose: () => void }>) {
  const reach = gap.reach;
  return (
    <Modal
      open
      size="lg"
      eyebrow={`Gap · ${KIND_LABEL[gap.kind]}`}
      title={gap.title}
      onClose={onClose}
      footer={
        <Button variant="glass" onClick={onClose}>
          Close
        </Button>
      }
    >
      <div className="gap-meta" data-testid="gap-meta">
        <SeverityBadge level={SEVERITY_LEVEL[gap.severity]} label={SEVERITY_LABEL[gap.severity]} />
        <Tag>{KIND_LABEL[gap.kind]}</Tag>
        <Tag>{ORIGIN_LABEL[gap.origin]}</Tag>
        {gap.status === "open" ? null : <Tag tone="success">{STATUS_LABEL[gap.status]}</Tag>}
        {gap.blocking ? <Tag tone="warning">Holds tasks</Tag> : null}
      </div>
      <GapQuestion gap={gap} />
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
      {gap.clarification === null ? null : (
        <section className="gap-sec" aria-label="Answer" data-testid="gap-answer">
          <h3 className="gap-sec__title eyebrow">Answer</h3>
          <p className="gap-text">{clarificationText(gap.clarification)}</p>
          <p className="gap-quote__src body-sm">
            {gap.clarification.mode === "ai" ? "Answered by AI" : `Answered by ${gap.clarification.answeredBy}`}
          </p>
        </section>
      )}
    </Modal>
  );
}
