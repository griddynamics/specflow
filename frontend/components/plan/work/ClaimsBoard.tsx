"use client";
// MCP-TOOLING session 10: the claims board. One row per story claim (who, story or task, branch, since, "Release claim"; claims older than a week are marked) and per task
// claimed on its own (listed; a task claim has no forced release). Oldest first, so a forgotten claim is at the top. States: skeleton,
// failure with "Try again", empty, the board. `refresh` re-reads after an answer or a release.
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { DataTable } from "@/components/ui/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/Icon";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";
import { tabStop } from "@/components/ui/utils";
import type { Claim } from "@/lib/domain/work";
import { useWorkPort } from "../use-plan-ports";
import { ReleaseClaimDialog } from "./ReleaseClaimDialog";
import { age, formatWhen, isStale, isStoryClaim, personName, taskCount } from "./work-logic";

const COLUMNS = ["Who", "Story or task", "Branch", "Since", "Action"] as const;

function ClaimRow({ claim, now, onRelease }: Readonly<{ claim: Claim; now: number; onRelease: () => void }>) {
  const story = isStoryClaim(claim);
  const old = isStale(claim.claimedAt, now);
  return (
    <tr className="work-claim" data-testid="claim" data-story={claim.storyId} data-kind={claim.kind} data-stale={old ? "" : undefined}>
      <td className="work-claim__who">
        <span className="work-claim__name" data-testid="claim-holder">
          {personName(claim.holder)}
        </span>
        {claim.holder.agentLabel === null ? null : <Tag tone="info">{claim.holder.agentLabel}</Tag>}
      </td>
      <td className="work-claim__what">
        <span className="work-claim__title">{claim.title}</span>
        <span className="work-claim__sub body-sm">
          {story ? `Story ${claim.storyId} · ${taskCount(claim.tasks)}` : `Task ${claim.taskId ?? ""} · story ${claim.storyId}`}
        </span>
      </td>
      <td className="work-claim__branch body-sm">
        {claim.branch === null ? null : (
          <span className="work-claim__branch-line">
            <Icon name="git-branch" size={16} />
            <code data-testid="claim-branch">{claim.branch}</code>
          </span>
        )}
      </td>
      <td className="work-claim__since body-sm">
        <time dateTime={claim.claimedAt} title={formatWhen(claim.claimedAt)} data-testid="claim-since">
          {age(claim.claimedAt, now)}
        </time>
        {old ? <Tag tone="warning">Old</Tag> : null}
      </td>
      <td className="work-claim__actions">
        {story ? (
          <Button size="sm" variant="glass" data-testid="claim-release" aria-label={`Release claim on ${claim.title}`} onClick={onRelease}>
            Release claim
          </Button>
        ) : (
          <span className="work-claim__sub body-sm" data-testid="claim-task-note">
            Task claim: released by its agent
          </span>
        )}
      </td>
    </tr>
  );
}

/** The board, or what stands in for it: the skeleton, the failure with "Try again", the empty note. */
function ClaimsBody({
  claims,
  now,
  onRetry,
  onRelease,
}: Readonly<{ claims: Claim[] | "failed" | null; now: number; onRetry: () => void; onRelease: (claim: Claim) => void }>) {
  if (claims === null) {
    return (
      <div aria-hidden>
        <Skeleton variant="block" height={96} />
      </div>
    );
  }
  if (claims === "failed") {
    return (
      <EmptyState title="Claims could not be loaded" icon="warning-circle" actions={<Button onClick={onRetry}>Try again</Button>}>
        Check your connection and try again.
      </EmptyState>
    );
  }
  if (claims.length === 0) {
    return (
      <EmptyState title="Nobody is working on a story" icon="flag-checkered">
        When an agent claims a story, who holds it and since when shows here. A forgotten claim can be released.
      </EmptyState>
    );
  }
  return (
    <div className="work-table" role="region" aria-label="Claims" {...tabStop(true)} data-testid="claims-list">
      <DataTable>
        <thead>
          <tr>
            {COLUMNS.map((name) => (
              <th key={name} scope="col" className="body-sm">
                {name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {claims.map((claim) => (
            <ClaimRow
              key={`${claim.kind}:${claim.storyId}:${claim.taskId ?? ""}`}
              claim={claim}
              now={now}
              onRelease={() => {
                onRelease(claim);
              }}
            />
          ))}
        </tbody>
      </DataTable>
    </div>
  );
}

export function ClaimsBoard({ projectId, refresh, onChanged }: Readonly<{ projectId: string; refresh: number; onChanged: () => void }>) {
  const work = useWorkPort();
  const [claims, setClaims] = useState<Claim[] | "failed" | null>(null);
  const [tries, setTries] = useState(0);
  const [releasing, setReleasing] = useState<Claim | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let alive = true;
    work.claims(projectId).then(
      (rows) => {
        if (!alive) return;
        setNow(Date.now());
        setClaims(rows);
      },
      () => {
        if (alive) setClaims("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [work, projectId, refresh, tries]);

  return (
    <section className="work-section" data-testid="work-claims" aria-labelledby="work-claims-title" aria-busy={claims === null}>
      <div className="work-section__head">
        <h2 className="work-section__title title-3" id="work-claims-title">
          Claims
        </h2>
      </div>
      <ClaimsBody
        claims={claims}
        now={now}
        onRetry={() => {
          setClaims(null);
          setTries((n) => n + 1);
        }}
        onRelease={setReleasing}
      />
      {releasing === null ? null : (
        <ReleaseClaimDialog
          projectId={projectId}
          claim={releasing}
          onClose={() => {
            setReleasing(null);
          }}
          onDone={() => {
            setReleasing(null);
            onChanged();
          }}
        />
      )}
    </section>
  );
}
