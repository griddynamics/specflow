"use client";
// FR-GWEB-04 (api.md "User interface", Gaps screen, noise report), read-only: the sections the latest finished gap run judged tracking or
// mixed, with how it was decided, their signals and how many nodes they removed from the checks. "Mark as not spec" and the noise rules
// come with the write routes. `503 gap_unavailable` shows the "not available" state; another failure offers a retry.
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";
import { usePorts } from "@/lib/data/provider";
import type { GapNoise } from "@/lib/domain/gaps";
import { DECIDED_BY_LABEL, RELEVANCE_LABEL, failureOf, headingText, removedText } from "./gaps-logic";
import { GapsUnavailable } from "./GapsUnavailable";

type Load = GapNoise | "unavailable" | "failed" | null;

/** The latest gap run's noise report; `tries` reads it again. */
function useNoise(projectId: string, tries: number): Load {
  const { gaps } = usePorts();
  const [data, setData] = useState<Load>(null);
  useEffect(() => {
    let alive = true;
    gaps.noise(projectId).then(
      (n) => {
        if (alive) setData(n);
      },
      (e: unknown) => {
        if (alive) setData(failureOf(e));
      },
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId, tries]);
  return data;
}

export function NoiseReport({ projectId }: Readonly<{ projectId: string }>) {
  const [tries, setTries] = useState(0);
  const data = useNoise(projectId, tries);
  if (data === null) return <Skeleton variant="block" height={200} />;
  if (data === "unavailable") return <GapsUnavailable />;
  if (data === "failed") {
    return (
      <EmptyState
        title="The noise report could not be loaded"
        icon="warning-circle"
        actions={
          <Button
            onClick={() => {
              setTries((n) => n + 1);
            }}
          >
            Try again
          </Button>
        }
      >
        Check your connection and try again.
      </EmptyState>
    );
  }
  if (data.runId === null) {
    return (
      <div data-testid="noise-none">
        <EmptyState title="No gap run has finished yet" icon="funnel-simple">
          The noise report lists the sections a gap run set aside as tracking notes rather than specification.
        </EmptyState>
      </div>
    );
  }
  if (data.sections.length === 0) {
    return (
      <div data-testid="noise-empty">
        <EmptyState title="No noise found" icon="check-circle">
          The latest gap run read every section as specification.
        </EmptyState>
      </div>
    );
  }
  return (
    <GlassSurface variant="strong" className="noise">
      <ul className="noise__list" aria-label="Noise report" data-testid="noise-report">
        {data.sections.map((s) => (
          <li key={`${s.lineage}|${s.headingPath.join("/")}`} className="noise__row" data-testid="noise-section">
            <div className="noise__head">
              <h3 className="noise__title headline">{headingText(s)}</h3>
              <Tag tone={s.relevance === "tracking" ? "pending" : "warning"}>{RELEVANCE_LABEL[s.relevance]}</Tag>
            </div>
            <p className="noise__meta body-sm">
              {DECIDED_BY_LABEL[s.by]} · {removedText(s.removed.length)}
            </p>
            {s.signals.length === 0 ? null : (
              <ul className="gap-nodes__list" aria-label="Signals">
                {s.signals.map((signal) => (
                  <li key={signal}>
                    <Tag>{signal}</Tag>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </GlassSurface>
  );
}
