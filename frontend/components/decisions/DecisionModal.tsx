"use client";
// FE-DEC-03, FE-DEC-04, FE-DOD-05.AC3/AC4 (PLAN a.8, b.6): the decision modal. Context (.gdg-context), Sources (DocLinks), Questions
// (QuestionBlock per question). An open decision takes answers (choice with "D: Other" text box, Yes/No, a visual-only drop zone, one
// optional comment per question, PA26) and Submit/Cancel; an answered one shows its recorded answers read-only with an "Answered"
// badge and no Submit (AC8). The modal is the P04 Modal (focus trap, Esc, focus returns to the opener). The drafts live here;
// nothing is written anywhere until Submit, and Submit writes nothing to the list (FE-DEC-04).
import { Fragment, useState } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Button } from "@/components/ui/Button";
import { DocLinks } from "@/components/ui/DocLinks";
import { Modal } from "@/components/ui/Modal";
import { QuestionBlock } from "@/components/ui/QuestionBlock";
import { SeverityBadge } from "@/components/ui/SeverityBadge";
import { Tag } from "@/components/ui/Tag";
import type { DocRef } from "@/lib/domain/common";
import type { AnswerValue, Decision, DecisionAnswer } from "@/lib/domain/decisions";
import { buildAnswer, docLinkParts, emptyDraft, hasAnswer, recordedSummary, type QuestionDraft } from "./decisions-logic";
import "./dec-gaps.css";

/** The file name shown on an upload question: the recorded answer's, or the draft's files while the question is open. */
function fileNameOf(recorded: AnswerValue | undefined, draft: QuestionDraft | undefined): string | undefined {
  if (recorded !== undefined) return recorded.kind === "files" ? recordedSummary(recorded) : undefined;
  const joined = draft?.files.join(", ");
  return joined === undefined || joined === "" ? undefined : joined;
}

export function DecisionModal({
  decision,
  assigneeName,
  submitting,
  onClose,
  onSubmit,
  onOpenDoc,
}: Readonly<{
  decision: Decision;
  assigneeName?: string;
  submitting?: boolean;
  onClose: () => void;
  onSubmit: (answer: DecisionAnswer) => void;
  onOpenDoc: (ref: DocRef) => void;
}>) {
  const answered = decision.status === "answered";
  const [drafts, setDrafts] = useState<Record<string, QuestionDraft | undefined>>({});
  const patch = (qid: string, change: Partial<QuestionDraft>) => {
    setDrafts((d) => ({ ...d, [qid]: { ...(d[qid] ?? emptyDraft()), ...change } }));
  };
  const paragraphs = decision.context
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean);
  const links = decision.docLinks.map((ref) => ({
    ...docLinkParts(ref),
    onClick: () => {
      onOpenDoc(ref);
    },
  }));

  return (
    <Modal
      open
      size="lg"
      eyebrow={`Decision · ${decision.domain}`}
      title={decision.title}
      onClose={onClose}
      footer={
        answered ? (
          <Button variant="glass" onClick={onClose}>
            Close
          </Button>
        ) : (
          <>
            <Button variant="glass" onClick={onClose}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={submitting}
              onClick={() => {
                onSubmit(buildAnswer(decision, drafts));
              }}
            >
              Submit
            </Button>
          </>
        )
      }
    >
      <div className="dec-meta" data-testid="decision-meta">
        <SeverityBadge level={decision.severity} />
        <Tag>{decision.domain}</Tag>
        {assigneeName !== undefined && assigneeName !== "" ? (
          <span className="dec-meta__who body-sm">
            <Avatar name={assigneeName} size="sm" title={false} />
            {assigneeName}
          </span>
        ) : null}
        {answered ? <Tag tone="success">Answered</Tag> : null}
      </div>
      <section className="dec-sec" aria-label="Context">
        <h3 className="dec-sec__title eyebrow">Context</h3>
        <div className="gdg-context dec-context">
          {paragraphs.map((p, i) => (
            <p key={i}>{p}</p>
          ))}
        </div>
      </section>
      {links.length ? (
        <section className="dec-sec" aria-label="Sources">
          <h3 className="dec-sec__title eyebrow">Sources</h3>
          <DocLinks links={links} />
        </section>
      ) : null}
      <section className="dec-sec" aria-label="Questions" data-testid="decision-questions">
        <h3 className="dec-sec__title eyebrow">Questions</h3>
        <div>
          {decision.questions.map((q) => {
            const rec = answered ? decision.recorded?.[q.id] : undefined;
            const a = rec?.answer;
            const orphanComment = !answered && (drafts[q.id]?.comment.trim() ?? "") !== "" && !hasAnswer(q, drafts[q.id]);
            return (
              <Fragment key={q.id}>
                <QuestionBlock
                  number={q.number}
                  question={q.text}
                  help={q.help}
                  type={q.type}
                  options={q.options}
                  allowOther={q.allowOther}
                  readOnly={answered}
                  defaultValue={a && (a.kind === "option" || a.kind === "yesno") ? a.value : undefined}
                  defaultOther={a?.kind === "option" ? a.otherText : undefined}
                  fileName={fileNameOf(a, drafts[q.id])}
                  defaultComment={rec?.comment}
                  onChange={(v) => {
                    patch(q.id, q.type === "yesno" ? { yesno: v === "yes" ? "yes" : "no" } : { choice: v });
                  }}
                  onOtherChange={(text) => {
                    patch(q.id, { other: text });
                  }}
                  onFiles={(files) => {
                    patch(q.id, { files: files.map((f) => f.name) });
                  }}
                  onCommentChange={(text) => {
                    patch(q.id, { comment: text });
                  }}
                />
                {orphanComment ? (
                  <p className="dec-hint body-sm" data-testid="comment-hint">
                    Answer the question for your comment to be sent
                  </p>
                ) : null}
              </Fragment>
            );
          })}
        </div>
      </section>
    </Modal>
  );
}
