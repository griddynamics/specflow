"use client";
// FE-PLAN-08 (TECH-SPECS §2.15; DECISIONS P20-11, P25-1..5): the Develop tab. Illustrative only: no MCP server exists. Reads
// `plan.develop(projectId)` with `usePorts()`; no fetch of its own (R10). Order: banner (always first), "Agent" picker, three steps
// (Connect, Teach the agent, Run it), four workflow cards, the tool table. The slot props are fixed by P21 (`{ projectId }`).
import { useEffect, useState } from "react";
import { Card } from "@/components/ui/Card";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";
import { usePlanPort } from "../use-plan-ports";
import type { AgentId, DevelopContent } from "@/lib/domain/plan";
import { CodeBlock } from "./CodeBlock";
import { AGENT_OPTIONS, CONNECT_CAPTION, teachCaption } from "./labels";
import "./develop.css";
import { DataTable } from "@/components/ui/DataTable";

/** The tab below the banner: the failure note, the skeleton, or the agent picker with the steps, workflows and tools. */
function DevelopBody({
  content,
  agentId,
  onAgent,
}: Readonly<{ content: DevelopContent | "failed" | null; agentId: AgentId; onAgent: (id: AgentId) => void }>) {
  if (content === "failed") return <p role="status">Could not load this tab</p>;
  const agent = content === null ? undefined : (content.agents.find((a) => a.id === agentId) ?? content.agents[0]);
  if (content === null || !agent) {
    return (
      <div aria-hidden>
        <Skeleton variant="block" height={280} />
      </div>
    );
  }
  return (
    <>
      <SegmentedControl
        label="Agent"
        options={AGENT_OPTIONS}
        value={agentId}
        onChange={(v) => {
          onAgent(v as AgentId);
        }}
      />
      <div className="dev-steps" data-testid="develop-steps">
        <Card variant="strong" eyebrow="Step 1" title="Connect">
          <CodeBlock key={agent.id} caption={CONNECT_CAPTION[agent.id]} text={agent.config} />
        </Card>
        <Card variant="strong" eyebrow="Step 2" title="Teach the agent">
          <CodeBlock key={agent.id} caption={teachCaption(agent.id, agent.teachFile.path)} text={agent.teachFile.body} />
        </Card>
        <Card variant="strong" eyebrow="Step 3" title="Run it">
          <CodeBlock key={agent.id} caption="Say this to the agent" text={agent.run} />
        </Card>
      </div>
      <h2 className="gdg-card__title">Workflows</h2>
      <div className="dev-flows" data-testid="develop-workflows">
        {content.workflows.map((w, i) => (
          <Card variant="strong" key={w.title} eyebrow={`Workflow ${String(i + 1)}`} title={w.title}>
            <ol>
              {w.steps.map((s) => (
                <li key={s}>{withCode(s)}</li>
              ))}
            </ol>
            {i === 0 && content.nextTask ? (
              <p className="dev-example" data-testid="develop-next-task">
                Example next task: {content.nextTask.title}
              </p>
            ) : null}
          </Card>
        ))}
      </div>
      <h2 className="gdg-card__title">Tools</h2>
      <div className="dev-tools" data-testid="develop-tools">
        <DataTable>
          <thead>
            <tr>
              <th scope="col">Tool</th>
              <th scope="col">Backed by</th>
              <th scope="col">What it does</th>
            </tr>
          </thead>
          <tbody>
            {content.tools.map((t) => (
              <tr key={t.name}>
                <td>
                  <code>{t.name}</code>
                </td>
                <td>
                  <code>{t.method}</code>
                </td>
                <td>{t.note}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      </div>
    </>
  );
}

export function DevelopTab({ projectId }: Readonly<{ projectId: string }>) {
  const plan = usePlanPort();
  const [content, setContent] = useState<DevelopContent | "failed" | null>(null);
  const [agentId, setAgentId] = useState<AgentId>("claude-code");

  useEffect(() => {
    let alive = true;
    plan.develop(projectId).then(
      (c) => {
        if (alive) setContent(c);
      },
      () => {
        if (alive) setContent("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [plan, projectId]);

  return (
    <div className="dev" data-testid="plan-develop" data-project={projectId} data-agent={agentId}>
      <p className="dev-banner" data-testid="develop-banner">
        <Tag tone="warning">Illustrative</Tag>
        <span>No MCP server exists yet. These snippets show how a coding agent would connect.</span>
      </p>
      <DevelopBody content={content} agentId={agentId} onAgent={setAgentId} />
    </div>
  );
}

/** Backtick spans of a content line become `<code>`; the rest stays text. */
function withCode(line: string) {
  return line.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part));
}
