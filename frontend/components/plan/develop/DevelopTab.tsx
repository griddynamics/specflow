// MCP-M10 (FR-MCP-UI-07, 08): Plan › Develop, slim and per project. It binds one repository to this project: the bind command with the
// project id, the binding file as a disclosure, two sample prompts, and the way to the Guides. It takes plain props from the plan page
// (E11); it reads no config and fetches nothing.
import Link from "next/link";
import { Card } from "@/components/ui/Card";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { DEVELOP } from "@/lib/guides/content";
import { bindCommand, bindingFileJson, DEVELOP_OVERVIEW_PROMPT, DEVELOP_START_PROMPT } from "@/lib/guides/snippets";
import { routesFor } from "@/lib/routes";
import "@/components/guides/guides.css";
import "./develop.css";

/** What the plan page reads on the server and hands down (E11). `projectName` is null when the project read failed. */
export interface DevelopInfo {
  projectName: string | null;
  mcpUrl: string;
  serverName: string;
}

export function DevelopTab({ projectId, info }: Readonly<{ projectId: string; info: DevelopInfo }>) {
  const { projectName, mcpUrl, serverName } = info;
  const guide = routesFor("production").guides("mcp-server");
  return (
    <div className="dev" data-testid="plan-develop" data-project={projectId}>
      {projectName === null ? (
        <p role="status">{DEVELOP.nameMissing}</p>
      ) : (
        <Card variant="strong" eyebrow={DEVELOP.bind.eyebrow} title={DEVELOP.bind.title(projectName)}>
          <div className="guides-step">
            <p>{DEVELOP.bind.text}</p>
            <CodeBlock caption={DEVELOP.bind.caption} text={bindCommand(serverName, projectId)} />
            <details className="guides-details">
              <summary className="gdg-hit guides-details__summary">{DEVELOP.bind.file.summary}</summary>
              <div className="guides-details__body">
                <CodeBlock caption={DEVELOP.bind.file.caption} text={bindingFileJson({ mcpUrl, projectId, projectName })} />
              </div>
            </details>
          </div>
        </Card>
      )}
      <Card variant="strong" eyebrow={DEVELOP.ask.eyebrow} title={DEVELOP.ask.title}>
        <div className="guides-step">
          <CodeBlock caption={DEVELOP.ask.overview} text={DEVELOP_OVERVIEW_PROMPT} />
          <CodeBlock caption={DEVELOP.ask.start} text={DEVELOP_START_PROMPT} />
        </div>
      </Card>
      <p className="dev-guide">
        <span>{DEVELOP.notConnected}</span>
        <Link href={guide} className="dev-guide__link">
          {DEVELOP.guideLink}
        </Link>
      </p>
    </div>
  );
}
