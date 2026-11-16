"use client";
// FR-GWEB-04 (answers.md "Review list", "Revert"): the "Answered by AI" tab. Every AI and pointer answer, newest first: the gap's title (a
// link to its card), the answer, the basis quotes, the confidence in words, the build that applied it and its status. "Revert" per row (a
// reason 1..500) withdraws it; the list reads again and a reverted row names the follow-up gap a person answers instead, as a link to its
// card. Nothing waits for a review here: the list exists so a person can check and undo. `503 gap_unavailable` shows "not available".
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";
import type { AiAnswer, GapsPort } from "@/lib/domain/gaps";
import { ReasonDialog } from "./GapDialogs";
import { GapLink } from "./GapLink";
import { GapsUnavailable } from "./GapsUnavailable";
import { CLARIFICATION_STATUS_LABEL, clarificationText, confidenceText, failureOf, whenText } from "./gaps-logic";

type Load = AiAnswer[] | "unavailable" | "failed" | null;

const REVERTIBLE = new Set(["pending", "applied", "absorbed"]);

function AiRow({
  row,
  href,
  onOpenGap,
  onRevert,
}: Readonly<{ row: AiAnswer; href: (id: string) => string; onOpenGap: (id: string) => void; onRevert: () => void }>) {
  const c = row.clarification;
  const confidence = confidenceText(c.confidence);
  const basis = c.basis ?? [];
  return (
    <li className="ai-row" data-testid="ai-answer-row" data-gap={row.gapId} data-status={c.status}>
      <div className="ai-row__head">
        <h3 className="ai-row__title headline">
          <GapLink
            href={href(row.gapId)}
            onOpen={() => {
              onOpenGap(row.gapId);
            }}
            testId="ai-answer-link"
          >
            {row.title === "" ? row.gapId : row.title}
          </GapLink>
        </h3>
        <Tag tone={c.status === "reverted" ? "warning" : "neutral"}>{CLARIFICATION_STATUS_LABEL[c.status]}</Tag>
        {c.mode === "pointer" ? <Tag>Found in the sources</Tag> : null}
      </div>
      {row.question === "" ? null : <p className="ai-row__q body-sm">{row.question}</p>}
      <p className="gap-text" data-testid="ai-answer-text">
        {clarificationText(c)}
      </p>
      <p className="ai-row__meta body-sm" data-testid="ai-answer-meta">
        {[
          confidence,
          c.appliedBuildId === null || c.appliedBuildId === undefined ? "Not applied yet" : `Applied by build ${c.appliedBuildId}`,
          `answered ${whenText(c.answeredAt)}`,
        ]
          .filter((x) => x !== null)
          .join(" · ")}
      </p>
      {basis.length === 0 ? null : (
        <ul className="gap-quotes" aria-label="Basis" data-testid="ai-answer-basis">
          {basis.map((b, i) => (
            <li key={`${b.fragmentId}-${String(i)}`}>
              <blockquote className="gap-quote">{b.quote}</blockquote>
              <span className="gap-quote__src body-sm">{b.fragmentId}</span>
            </li>
          ))}
        </ul>
      )}
      {row.followUp === null ? null : (
        <p className="body-sm" data-testid="ai-answer-follow-up">
          A person answers it now in the follow-up gap{" "}
          <GapLink
            href={href(row.followUp)}
            onOpen={() => {
              onOpenGap(row.followUp ?? "");
            }}
            testId="ai-follow-up-link"
          >
            {row.followUp}
          </GapLink>
        </p>
      )}
      {REVERTIBLE.has(c.status) ? (
        <div>
          <Button
            variant="glass"
            size="sm"
            onClick={onRevert}
            aria-label={`Revert the AI answer to ${row.title === "" ? row.gapId : row.title}`}
            data-testid="ai-revert"
          >
            Revert
          </Button>
        </div>
      ) : null}
    </li>
  );
}

export function AiAnswers({
  gaps,
  projectId,
  href,
  onOpenGap,
  onChanged,
}: Readonly<{
  gaps: GapsPort;
  projectId: string;
  href: (id: string) => string;
  onOpenGap: (id: string) => void;
  onChanged: () => void;
}>) {
  const [data, setData] = useState<Load>(null);
  const [tries, setTries] = useState(0);
  const [reverting, setReverting] = useState<AiAnswer | null>(null);
  const [done, setDone] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    gaps.aiAnswers(projectId).then(
      (rows) => {
        if (alive) setData(rows);
      },
      (e: unknown) => {
        if (alive) setData(failureOf(e));
      },
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId, tries]);
  const again = useCallback(() => {
    setTries((n) => n + 1);
  }, []);

  if (data === null) return <Skeleton variant="block" height={200} />;
  if (data === "unavailable") return <GapsUnavailable />;
  if (data === "failed") {
    return (
      <EmptyState title="The AI answers could not be loaded" icon="warning-circle" actions={<Button onClick={again}>Try again</Button>}>
        Check your connection and try again.
      </EmptyState>
    );
  }
  return (
    <div className="ai-answers" data-testid="ai-answers">
      <p className="gaps-count body-sm">
        Gaps that Specflow answered itself, with the lines it relied on. Nothing waits for your review; revert any answer you disagree with.
      </p>
      {done === null ? null : (
        <p className="gap-done body-sm" role="status" data-testid="ai-revert-result">
          {done}
        </p>
      )}
      {data.length === 0 ? (
        <div data-testid="ai-answers-empty">
          <EmptyState title="No AI answers" icon="check-circle">
            When a gap run is sure of an answer from the sources, it answers the gap and lists it here.
          </EmptyState>
        </div>
      ) : (
        <GlassSurface variant="strong" className="noise">
          <ul className="noise__list" aria-label="Answered by AI">
            {data.map((row) => (
              <AiRow
                key={row.clarification.clrId}
                row={row}
                href={href}
                onOpenGap={onOpenGap}
                onRevert={() => {
                  setReverting(row);
                }}
              />
            ))}
          </ul>
        </GlassSurface>
      )}
      {reverting === null ? null : (
        <ReasonDialog
          title="Revert AI answer"
          description="The answer's text leaves the specification with the next build, and a person answers the question instead."
          confirmLabel="Revert"
          onClose={() => {
            setReverting(null);
          }}
          onConfirm={async (reason) => {
            const r = await gaps.revert(projectId, reverting.clarification.clrId, reason);
            setReverting(null);
            setDone(
              r.followUp
                ? "Answer reverted. A follow-up gap is open for a person; it is linked on the row."
                : "Answer reverted. The gap is open again for a person in the inbox.",
            );
            onChanged();
            again();
          }}
        />
      )}
    </div>
  );
}
