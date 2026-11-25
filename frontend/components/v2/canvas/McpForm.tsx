"use client";
// MCP joins the canvas by itself (under Gap when the gaps are found, under Plan when the plan is built); there is nothing to set up. This is
// how a coding agent connects, with the tools of the stage. The commands are the Guides' own (`lib/guides/snippets.ts`).
import { Button, CodeLine, CopyRow, DialogFoot, Notice, Row, SectionHead, Stack, StepList } from "@graphico/ui";
import { bindCommand, claudeAddCommand, SIGN_IN_COMMAND } from "@/lib/guides/snippets";
import type { FormProps } from "../connector-types";
import { CANVAS_COPY, MCP_TOOLS } from "./canvas-copy";

const COPY_ROW = { copyLabel: CANVAS_COPY.driveCopy, copiedLabel: CANVAS_COPY.driveCopied } as const;

export function McpForm({ projectId, zone, mcp, onClose }: Readonly<FormProps>) {
  const stage = zone === "plan" ? "plan" : "gap";
  return (
    <Stack>
      <Notice>{CANVAS_COPY.mcpSay[stage]}</Notice>
      <StepList>
        <li>{CANVAS_COPY.mcpStepAdd}</li>
      </StepList>
      <CopyRow {...COPY_ROW} text={claudeAddCommand(mcp)} name="Claude Code command" />
      <StepList start={2}>
        <li>{CANVAS_COPY.mcpStepSignIn}</li>
      </StepList>
      <CopyRow {...COPY_ROW} text={SIGN_IN_COMMAND} name="sign-in command" />
      <StepList start={3}>
        <li>{CANVAS_COPY.mcpStepBind}</li>
      </StepList>
      <CopyRow {...COPY_ROW} text={bindCommand(mcp.serverName, projectId)} name="bind command" />
      <Notice>{CANVAS_COPY.mcpOther}</Notice>
      <SectionHead title={CANVAS_COPY.mcpTools} />
      <Row>
        {MCP_TOOLS[stage].map((tool) => (
          <CodeLine key={tool} inline>
            {tool}
          </CodeLine>
        ))}
      </Row>
      <DialogFoot>
        <span />
        <Button variant="ghost" onClick={onClose}>
          {CANVAS_COPY.done}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
