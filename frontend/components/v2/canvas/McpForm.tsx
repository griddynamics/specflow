"use client";
// MCP joins the canvas by itself (under Gap when the gaps are found, under Plan when the plan is built); there is nothing to set up. The dialog
// says what the stage's tools do for a coding agent; how to connect one is the MCP dialog of the Integrations page, one link away.
import { Button, DialogFoot, Notice, SectionHead, ShowMeHow, Stack, ToolGrid } from "@graphico/ui";
import type { FormProps } from "../connector-types";
import { useV2Routes } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY, MCP_TOOLS } from "./canvas-copy";

export function McpForm({ projectId, zone, onClose }: Readonly<FormProps>) {
  const routes = useV2Routes();
  const stage = zone === "plan" ? "plan" : "gap";
  return (
    <Stack>
      <Notice>{CANVAS_COPY.mcpSay[stage]}</Notice>
      <ShowMeHow label={V2_COPY.showMeHow} href={routes.integrations(projectId, { setup: "mcp", next: routes.canvas(projectId) })} />
      <SectionHead title={CANVAS_COPY.mcpTools} />
      <ToolGrid label={CANVAS_COPY.mcpToolsLabel} items={MCP_TOOLS[stage]} />
      <DialogFoot>
        <span />
        <Button variant="ghost" onClick={onClose}>
          {CANVAS_COPY.done}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
