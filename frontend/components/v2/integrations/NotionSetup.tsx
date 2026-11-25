"use client";
// Notion: connect with the caller's own account (per-user OAuth) and bind it to the project. Notion's consent page has the page picker, so
// pages are shared there (connect again to share more). Stream C1/C2's UI owner (C2) fills this file; this is the minimal valid placeholder.
import { Notice } from "@graphico/ui";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";
import { CANVAS_COPY } from "../canvas/canvas-copy";

export function NotionSetup(props: Readonly<SetupProps>) {
  return (
    <SetupFrame props={props}>
      <Notice>{CANVAS_COPY.howNotion[0]}</Notice>
    </SetupFrame>
  );
}
