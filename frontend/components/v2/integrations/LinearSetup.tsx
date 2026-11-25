"use client";
// Linear: connect with the caller's own account (per-user OAuth) and bind it to the project. FOUNDATION STUB: stream S1 fills the body (sign-in,
// the workspace); until then the dialog only says Linear cannot be connected yet. Disconnect and "acts as" come from the shared frame.
import { Notice } from "@graphico/ui";
import { V2_COPY } from "../v2-copy";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";

export function LinearSetup(props: Readonly<SetupProps>) {
  return (
    <SetupFrame props={props}>
      <Notice>{V2_COPY.linearSetupSoon}</Notice>
    </SetupFrame>
  );
}
