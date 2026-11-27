"use client";
// FR-GWEB-04 (answers.md "Revert"; reconcile.md "Events"): the collapsible "History" of a gap's card. The events in time order, in words
// (who, what, when; a reconcile event says its outcome), never raw JSON; and the follow-ups as links to the other gap's card.
import { useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import type { GapHistory as History } from "@/lib/domain/gaps";
import { GapLink } from "./GapLink";
import { actorText, eventText, whenText } from "@/components/shared/gaps/gaps-logic";

const FOLLOW_UP_REASON: Record<string, string> = {
  not_fixed: "the answer did not fix it",
  orphaned: "its section is gone",
  conflict: "the source file changed the answer",
  reverted: "the answer was withdrawn",
};

export function GapHistory({
  history,
  names = {},
  href,
  onOpenGap,
}: Readonly<{
  history: History;
  /** Member names by user id: the history names the people who acted and the assignees. */
  names?: Readonly<Record<string, string>>;
  href: (gapId: string) => string;
  onOpenGap: (gapId: string) => void;
}>) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  if (history.events.length + history.followUps.length === 0) return null;
  return (
    <section className="gap-sec" aria-label="History" data-testid="gap-history-section">
      <div className="gap-sec__head">
        <h3 className="gap-sec__title eyebrow">History</h3>
        <Button
          variant="plain"
          size="sm"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => {
            setOpen((o) => !o);
          }}
          data-testid="gap-history-toggle"
        >
          {open ? "Hide history" : `Show history (${String(history.events.length)})`}
        </Button>
      </div>
      {history.followUps.length === 0 ? null : (
        <ul className="gap-follow" aria-label="Follow-ups" data-testid="gap-follow-ups">
          {history.followUps.map((f) => (
            <li key={`${f.direction}-${f.gapId}`} className="body-sm">
              {f.direction === "continued_by" ? "Followed up by " : "Follows up "}
              <GapLink
                href={href(f.gapId)}
                onOpen={() => {
                  onOpenGap(f.gapId);
                }}
                testId="gap-follow-up-link"
              >
                {f.gapId}
              </GapLink>
              {f.reason in FOLLOW_UP_REASON ? `: ${FOLLOW_UP_REASON[f.reason]}` : null}
            </li>
          ))}
        </ul>
      )}
      <ol id={listId} className="gap-events" hidden={!open} data-testid="gap-history">
        {history.events.map((e, i) => (
          <li key={`${e.at}-${e.event}-${String(i)}`} className="gap-event body-sm" data-testid="gap-history-event" data-event={e.event}>
            <span className="gap-event__what">{eventText(e, names)}</span>
            <span className="gap-event__meta">
              {actorText(e.actor, names)} · <time dateTime={e.at}>{whenText(e.at)}</time>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
