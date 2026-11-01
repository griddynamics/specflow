// MCP-M10 (FR-MCP-UI-03..06, 08): /guides/mcp-server. A server component: it gets plain props (the address, the client id, the server
// name, the agent) from the page, which reads the server-only config. One variant everywhere: the four steps with the team .mcp.json.
import type { ReactNode } from "react";
import { Card } from "@/components/ui/Card";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { DataTable } from "@/components/ui/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { tabStop } from "@/components/ui/utils";
import { agentLabel, isTested, type AgentId } from "@/lib/guides/agent";
import { MCP_GUIDE } from "@/lib/guides/content";
import manifest from "@/lib/guides/mcp-tools.json";
import {
  CHECK_PROMPT,
  claudeAddCommand,
  SIGN_IN_COMMAND,
  teamMcpJson,
} from "@/lib/guides/snippets";
import { routesFor } from "@/lib/routes";
import { AgentPicker } from "./AgentPicker";
import { ConnectPrompt } from "./ConnectPrompt";
import { GuideHeader } from "./GuideHeader";
import "@/components/ui/ui-gaps.css";
import "./guides.css";

export interface McpGuideProps {
  mcpUrl: string;
  clientId: string;
  serverName: string;
  agent: AgentId;
}

function Step({ eyebrow, title, children }: Readonly<{ eyebrow: string; title: string; children: ReactNode }>) {
  return (
    <Card variant="strong" eyebrow={eyebrow} title={title}>
      <div className="guides-step">{children}</div>
    </Card>
  );
}

const Note = ({ children }: Readonly<{ children: ReactNode }>) => <p className="body-sm guides-muted">{children}</p>;

function AddStep({ mcpUrl, clientId, serverName }: Readonly<Omit<McpGuideProps, "agent">>) {
  const s = MCP_GUIDE.steps;
  return (
    <Step eyebrow={s.add.eyebrow} title={s.add.title}>
      <p>{s.add.text}</p>
      <CodeBlock caption={s.add.caption} text={claudeAddCommand({ mcpUrl, clientId, serverName })} />
      <details className="guides-details">
        <summary className="gdg-hit guides-details__summary">{s.add.team.summary}</summary>
        <div className="guides-details__body">
          <p>{s.add.team.text}</p>
          <CodeBlock caption={s.add.team.caption} text={teamMcpJson({ mcpUrl, clientId, serverName })} />
        </div>
      </details>
    </Step>
  );
}

function SignInStep({ serverName }: Readonly<{ serverName: string }>) {
  const s = MCP_GUIDE.steps;
  return (
    <Step eyebrow={s.signIn.eyebrow} title={s.signIn.title}>
      <p>{s.signIn.text(serverName)}</p>
      <CodeBlock caption={s.signIn.caption} text={SIGN_IN_COMMAND} />
      <Note>{s.signIn.note(serverName)}</Note>
    </Step>
  );
}

function ClaudeCodeSteps(props: Readonly<Omit<McpGuideProps, "agent">>) {
  const s = MCP_GUIDE.steps;
  return (
    <div className="guides-stack" data-testid="mcp-steps">
      <AddStep {...props} />
      <SignInStep serverName={props.serverName} />
      <Step eyebrow={s.bind.eyebrow} title={s.bind.title}>
        <p>{s.bind.text}</p>
        <ConnectPrompt caption={s.bind.caption} />
        <Note>{s.bind.note}</Note>
      </Step>
      <Step eyebrow={s.check.eyebrow} title={s.check.title}>
        <p>{s.check.text}</p>
        <CodeBlock caption={s.check.caption} text={CHECK_PROMPT} />
      </Step>
    </div>
  );
}

function Reference({ serverName }: Readonly<{ serverName: string }>) {
  const r = MCP_GUIDE.reference;
  return (
    <Card variant="strong" eyebrow={r.eyebrow} title={r.title}>
      <div className="guides-step">
        <div className="guides-table" role="region" aria-label={r.tableLabel} {...tabStop(true)}>
          <DataTable>
            <thead>
              <tr>
                {r.columns.map((c) => (
                  <th key={c} scope="col">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {manifest.tools.map((t) => (
                <tr key={t.name}>
                  <td>
                    <code>{t.name}</code>
                  </td>
                  <td>{t.read_only ? r.toolKind : "Tool"}</td>
                  <td>{t.summary}</td>
                </tr>
              ))}
              {manifest.prompts.map((p) => (
                <tr key={p.name}>
                  <td>
                    <code>{`/mcp__${serverName}__${p.name}`}</code>
                  </td>
                  <td>{r.promptKind}</td>
                  <td>{p.summary}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        </div>
        <Note>{r.note}</Note>
      </div>
    </Card>
  );
}

function Troubleshooting({ serverName }: Readonly<{ serverName: string }>) {
  const t = MCP_GUIDE.troubleshooting;
  return (
    <Card variant="strong" eyebrow={t.eyebrow} title={t.title}>
      <div className="guides-trouble">
        {t.items(serverName).map((item) => (
          <details key={item.symptom} className="guides-details">
            <summary className="gdg-hit guides-details__summary">{item.symptom}</summary>
            <div className="guides-details__body">
              <p>{item.fix}</p>
            </div>
          </details>
        ))}
      </div>
    </Card>
  );
}

export function McpGuide({ mcpUrl, clientId, serverName, agent }: Readonly<McpGuideProps>) {
  const path = routesFor("production").guides("mcp-server");
  return (
    <div className="guides" data-testid="guide-mcp">
      <GuideHeader title={MCP_GUIDE.title} lead={MCP_GUIDE.lead} />
      <AgentPicker agent={agent} path={path} />
      {isTested(agent) ? (
        <ClaudeCodeSteps mcpUrl={mcpUrl} clientId={clientId} serverName={serverName} />
      ) : (
        <div data-testid="mcp-in-progress">
          <EmptyState variant="strong" icon="clock" title={MCP_GUIDE.inProgress.title(agentLabel(agent))}>
            {MCP_GUIDE.inProgress.text}
          </EmptyState>
        </div>
      )}
      <Reference serverName={serverName} />
      <Troubleshooting serverName={serverName} />
    </div>
  );
}
