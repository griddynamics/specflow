// MCP-M10 (FR-MCP-UI-03..06, 08): /guides/mcp-server. A server component: it gets plain props (the address, the client id, the mode, the
// dev e-mail, the agent) from the page, which reads the server-only config. Production (`oidc`) shows the four steps with the team
// .mcp.json; the local variant (`dev`) shows `specflow-local` and the dev-token command.
import type { ReactNode } from "react";
import { Card } from "@/components/ui/Card";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { DataTable } from "@/components/ui/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { tabStop } from "@/components/ui/utils";
import { agentLabel, isTested, type AgentId } from "@/lib/guides/agent";
import { MCP_GUIDE, troubleshootingFix } from "@/lib/guides/content";
import manifest from "@/lib/guides/mcp-tools.json";
import {
  bindCommand,
  CHECK_PROMPT,
  claudeAddCommand,
  localAddCommand,
  serverName,
  SIGN_IN_COMMAND,
  teamMcpJson,
  type GuideMode,
} from "@/lib/guides/snippets";
import { routesFor } from "@/lib/routes";
import { AgentPicker } from "./AgentPicker";
import { GuideHeader } from "./GuideHeader";
import "@/components/ui/ui-gaps.css";
import "./guides.css";

export interface McpGuideProps {
  mode: GuideMode;
  mcpUrl: string;
  clientId: string;
  devEmail: string;
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

function AddStep({ mode, mcpUrl, clientId, devEmail }: Readonly<Omit<McpGuideProps, "agent">>) {
  const s = MCP_GUIDE.steps;
  if (mode === "dev") {
    return (
      <Step eyebrow={s.addLocal.eyebrow} title={s.addLocal.title}>
        <p>{s.addLocal.text}</p>
        <CodeBlock caption={s.addLocal.caption} text={localAddCommand({ mcpUrl, devEmail })} />
        <Note>{s.addLocal.note}</Note>
      </Step>
    );
  }
  return (
    <Step eyebrow={s.add.eyebrow} title={s.add.title}>
      <p>{s.add.text}</p>
      <CodeBlock caption={s.add.caption} text={claudeAddCommand({ mcpUrl, clientId })} />
      <details className="guides-details">
        <summary className="gdg-hit guides-details__summary">{s.add.team.summary}</summary>
        <div className="guides-details__body">
          <p>{s.add.team.text}</p>
          <CodeBlock caption={s.add.team.caption} text={teamMcpJson({ mcpUrl, clientId })} />
        </div>
      </details>
    </Step>
  );
}

function SignInStep({ mode }: Readonly<{ mode: GuideMode }>) {
  const s = MCP_GUIDE.steps;
  if (mode === "dev") {
    return (
      <Step eyebrow={s.signInLocal.eyebrow} title={s.signInLocal.title}>
        <p>{s.signInLocal.text}</p>
      </Step>
    );
  }
  return (
    <Step eyebrow={s.signIn.eyebrow} title={s.signIn.title}>
      <p>{s.signIn.text}</p>
      <CodeBlock caption={s.signIn.caption} text={SIGN_IN_COMMAND} />
      <Note>{s.signIn.note}</Note>
    </Step>
  );
}

function ClaudeCodeSteps(props: Readonly<Omit<McpGuideProps, "agent">>) {
  const s = MCP_GUIDE.steps;
  return (
    <div className="guides-stack" data-testid="mcp-steps">
      <AddStep {...props} />
      <SignInStep mode={props.mode} />
      <Step eyebrow={s.bind.eyebrow} title={s.bind.title}>
        <p>{s.bind.text}</p>
        <CodeBlock caption={s.bind.caption} text={bindCommand(props.mode)} />
        <Note>{s.bind.note}</Note>
      </Step>
      <Step eyebrow={s.check.eyebrow} title={s.check.title}>
        <p>{s.check.text}</p>
        <CodeBlock caption={s.check.caption} text={CHECK_PROMPT} />
      </Step>
    </div>
  );
}

function Reference({ mode }: Readonly<{ mode: GuideMode }>) {
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
                    <code>{`/mcp__${serverName(mode)}__${p.name}`}</code>
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

function Troubleshooting({ mode }: Readonly<{ mode: GuideMode }>) {
  const t = MCP_GUIDE.troubleshooting;
  return (
    <Card variant="strong" eyebrow={t.eyebrow} title={t.title}>
      <div className="guides-trouble">
        {t.items.map((item) => (
          <details key={item.symptom} className="guides-details">
            <summary className="gdg-hit guides-details__summary">{item.symptom}</summary>
            <div className="guides-details__body">
              <p>{troubleshootingFix(item.fix, serverName(mode))}</p>
            </div>
          </details>
        ))}
      </div>
    </Card>
  );
}

export function McpGuide({ mode, mcpUrl, clientId, devEmail, agent }: Readonly<McpGuideProps>) {
  const path = routesFor("production").guides("mcp-server");
  return (
    <div className="guides" data-testid="guide-mcp" data-mode={mode}>
      <GuideHeader title={MCP_GUIDE.title} lead={MCP_GUIDE.lead} />
      <AgentPicker agent={agent} path={path} local={mode === "dev"} />
      {isTested(agent) ? (
        <ClaudeCodeSteps mode={mode} mcpUrl={mcpUrl} clientId={clientId} devEmail={devEmail} />
      ) : (
        <div data-testid="mcp-in-progress">
          <EmptyState variant="strong" icon="clock" title={MCP_GUIDE.inProgress.title(agentLabel(agent))}>
            {MCP_GUIDE.inProgress.text}
          </EmptyState>
        </div>
      )}
      <Reference mode={mode} />
      <Troubleshooting mode={mode} />
    </div>
  );
}
