"use client";
// MCP-TOOLING session 10: one finding of the list. Blocking first (the list order), a Blocking tag and the kind as words, the text, the spec
// nodes it is about (a kit button that shows only the findings about that node: the web app has no node page), its external references as links,
// who wrote it and when, and "Resolve" / "Dismiss" for an open one. A closed one shows who answered, when and the note.
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Tag } from "@/components/ui/Tag";
import type { CloseAction, Finding } from "@/lib/domain/work";
import { blockedStory, formatWhen, KIND_LABELS, personName, REF_LABELS, refHref } from "./work-logic";

function FindingTags({ finding }: Readonly<{ finding: Finding }>) {
  const story = blockedStory(finding);
  return (
    <div className="work-finding__tags">
      {finding.blocking ? <Tag tone="danger">{story === null ? "Blocking" : `Blocking · ${story}`}</Tag> : null}
      <Tag>{KIND_LABELS[finding.kind]}</Tag>
      {finding.status === "resolved" ? <Tag tone="success">Resolved</Tag> : null}
      {finding.status === "dismissed" ? <Tag>Dismissed</Tag> : null}
    </div>
  );
}

function FindingRefs({ finding }: Readonly<{ finding: Finding }>) {
  return (
    <ul className="work-finding__refs" aria-label="References">
      {finding.refersTo.map((ref) => {
        const href = refHref(ref);
        return (
          <li key={`${ref.type}:${ref.value}`} className="work-ref body-sm">
            <span className="work-ref__type">{REF_LABELS[ref.type]}:</span>
            {href === null ? (
              <code className="work-ref__value">{ref.value}</code>
            ) : (
              <a className="work-ref__link" href={href} target="_blank" rel="noopener noreferrer" data-testid="finding-ref">
                {ref.value}
                <Icon name="arrow-square-out" size={16} />
              </a>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function FindingNodes({ finding, onNode }: Readonly<{ finding: Finding; onNode: (specId: string) => void }>) {
  return (
    <ul className="work-finding__about" aria-label="About spec nodes">
      {finding.about.map((specId) => (
        <li key={specId}>
          <Button
            size="sm"
            variant="glass"
            className="work-node"
            data-testid="finding-node"
            title={`Show the findings about ${specId}`}
            onClick={() => {
              onNode(specId);
            }}
          >
            <code>{specId}</code>
          </Button>
        </li>
      ))}
    </ul>
  );
}

function FindingFooter({ finding, onAction }: Readonly<{ finding: Finding; onAction: (action: CloseAction) => void }>) {
  if (finding.status !== "open") {
    const by = finding.resolvedBy === null ? "" : ` by ${personName(finding.resolvedBy)}`;
    const at = finding.resolvedAt === null ? "" : ` · ${formatWhen(finding.resolvedAt)}`;
    const said = finding.resolutionNote === null ? "" : `: ${finding.resolutionNote}`;
    return (
      <p className="work-finding__meta body-sm" data-testid="finding-closed">
        {finding.status === "resolved" ? "Resolved" : "Dismissed"}
        {by}
        {at}
        {said}
      </p>
    );
  }
  return (
    <div className="work-finding__actions">
      <Button
        size="sm"
        variant="glass"
        data-testid="finding-resolve"
        aria-label={`Resolve finding ${finding.findingId}`}
        onClick={() => {
          onAction("resolve");
        }}
      >
        Resolve
      </Button>
      <Button
        size="sm"
        variant="plain"
        data-testid="finding-dismiss"
        aria-label={`Dismiss finding ${finding.findingId}`}
        onClick={() => {
          onAction("dismiss");
        }}
      >
        Dismiss
      </Button>
    </div>
  );
}

export function FindingRow({
  finding,
  onAction,
  onNode,
}: Readonly<{ finding: Finding; onAction: (action: CloseAction) => void; onNode: (specId: string) => void }>) {
  return (
    <li
      className="work-finding"
      data-testid="finding"
      data-finding={finding.findingId}
      data-blocking={finding.blocking ? "" : undefined}
      data-status={finding.status}
    >
      <FindingTags finding={finding} />
      <p className="work-finding__text" data-testid="finding-text">
        {finding.text}
      </p>
      {finding.about.length > 0 ? <FindingNodes finding={finding} onNode={onNode} /> : null}
      {finding.refersTo.length > 0 ? <FindingRefs finding={finding} /> : null}
      <p className="work-finding__meta body-sm">
        {personName(finding.author)} · {formatWhen(finding.createdAt)}
      </p>
      <FindingFooter finding={finding} onAction={onAction} />
    </li>
  );
}
