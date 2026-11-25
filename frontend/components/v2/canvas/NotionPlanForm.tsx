"use client";
// Notion plan destination: the link of the page the plan database is created under, Check access, then Add destination (docs/notion/plan.md "In the
// canvas"). Stream E2 owns this file; this is the minimal valid placeholder that renders in the plus dialog.
import { Notice } from "@graphico/ui";
import type { FormProps } from "../connector-types";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

export function NotionPlanForm({ onBack }: Readonly<FormProps>) {
  return (
    <ConnectFrame onBack={onBack}>
      <Notice>{CANVAS_COPY.notionPlanNote}</Notice>
    </ConnectFrame>
  );
}
