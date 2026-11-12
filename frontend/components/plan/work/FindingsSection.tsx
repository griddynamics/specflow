"use client";
// MCP-TOOLING session 10: the findings list of the Plan page. The status filter (Open by default) and an optional node filter (set by a
// click on a spec node of a finding) go to the API; the list comes back blocking first. States: skeleton, failure with "Try again", empty,
// the list. `refresh` (a number) re-reads after an answer or a release elsewhere on the page.
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Skeleton } from "@/components/ui/Skeleton";
import type { CloseAction, Finding, FindingFilter, FindingList } from "@/lib/domain/work";
import { useWorkPort } from "../use-plan-ports";
import { CloseFindingDialog } from "./CloseFindingDialog";
import { FindingRow } from "./FindingRow";
import { EMPTY_FINDINGS, EMPTY_FINDINGS_HELP, FILTER_OPTIONS } from "./work-logic";

/** The list, or what stands in for it: the skeleton, the failure with "Try again", the empty note. */
function FindingsBody({
  list,
  status,
  onRetry,
  onAction,
  onNode,
}: Readonly<{
  list: FindingList | "failed" | null;
  status: FindingFilter;
  onRetry: () => void;
  onAction: (finding: Finding, action: CloseAction) => void;
  onNode: (specId: string) => void;
}>) {
  if (list === null) {
    return (
      <div aria-hidden>
        <Skeleton variant="block" height={96} />
      </div>
    );
  }
  if (list === "failed") {
    return (
      <EmptyState title="Findings could not be loaded" icon="warning-circle" actions={<Button onClick={onRetry}>Try again</Button>}>
        Check your connection and try again.
      </EmptyState>
    );
  }
  if (list.findings.length === 0) {
    return (
      <EmptyState title={EMPTY_FINDINGS[status]} icon="chat-circle-text">
        {EMPTY_FINDINGS_HELP[status]}
      </EmptyState>
    );
  }
  return (
    <>
      <ul className="work-list" data-testid="findings-list">
        {list.findings.map((finding) => (
          <FindingRow
            key={finding.findingId}
            finding={finding}
            onAction={(action) => {
              onAction(finding, action);
            }}
            onNode={onNode}
          />
        ))}
      </ul>
      {list.more ? (
        <p className="work-more body-sm" role="status">
          Showing the first {list.findings.length} findings. Filter by status or spec node to see the rest.
        </p>
      ) : null}
    </>
  );
}

export function FindingsSection({
  projectId,
  refresh,
  onChanged,
}: Readonly<{ projectId: string; refresh: number; onChanged: () => void }>) {
  const work = useWorkPort();
  const [status, setStatus] = useState<FindingFilter>("open");
  const [node, setNode] = useState<string | undefined>(undefined);
  const [list, setList] = useState<FindingList | "failed" | null>(null);
  const [tries, setTries] = useState(0);
  const [closing, setClosing] = useState<{ finding: Finding; action: CloseAction } | null>(null);

  useEffect(() => {
    let alive = true;
    work.findings(projectId, { status, node }).then(
      (found) => {
        if (alive) setList(found);
      },
      () => {
        if (alive) setList("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [work, projectId, status, node, refresh, tries]);

  const reload = () => {
    setList(null);
    setTries((n) => n + 1);
  };

  return (
    <section className="work-section" data-testid="work-findings" aria-labelledby="work-findings-title" aria-busy={list === null}>
      <div className="work-section__head">
        <h2 className="work-section__title title-3" id="work-findings-title">
          Findings
        </h2>
        <SegmentedControl
          label="Finding status"
          options={FILTER_OPTIONS}
          value={status}
          onChange={(v) => {
            setList(null);
            setStatus(v as FindingFilter);
          }}
        />
      </div>
      {node !== undefined ? (
        <p className="work-filter body-sm" data-testid="work-node-filter">
          <span>
            About <code>{node}</code>
          </span>
          <Button
            size="sm"
            variant="plain"
            onClick={() => {
              setList(null);
              setNode(undefined);
            }}
          >
            Show all
          </Button>
        </p>
      ) : null}
      <FindingsBody
        list={list}
        status={status}
        onRetry={reload}
        onAction={(finding, action) => {
          setClosing({ finding, action });
        }}
        onNode={(specId) => {
          setList(null);
          setNode(specId);
        }}
      />
      {closing === null ? null : (
        <CloseFindingDialog
          projectId={projectId}
          finding={closing.finding}
          action={closing.action}
          onClose={() => {
            setClosing(null);
          }}
          onDone={() => {
            setClosing(null);
            onChanged();
          }}
        />
      )}
    </section>
  );
}
