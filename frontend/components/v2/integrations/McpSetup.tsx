"use client";
// MCP on the Integrations page: how a coding agent connects, the three steps with their commands (`lib/guides/snippets.ts`).
// MCP is not an integration of the project, so this dialog has nothing to bind or disconnect: it is the instructions and Done.
import { Button, CopyRow, DialogFoot, Notice, StepList, Stack } from "@graphico/ui";
import { bindCommand, claudeAddCommand, SIGN_IN_COMMAND } from "@/lib/guides/snippets";
import type { McpAddress } from "../connector-types";
import { CANVAS_COPY } from "../canvas/canvas-copy";
import { V2_COPY } from "../v2-copy";

const COPY_ROW = { copyLabel: CANVAS_COPY.driveCopy, copiedLabel: CANVAS_COPY.driveCopied } as const;

export function McpSetup({ projectId, mcp, onDone }: Readonly<{ projectId: string; mcp: McpAddress; onDone: () => void }>) {
  return (
    <Stack>
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
      <DialogFoot>
        <span />
        <Button variant="ghost" onClick={onDone}>
          {V2_COPY.done}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
