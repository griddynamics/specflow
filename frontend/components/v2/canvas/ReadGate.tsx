"use client";
// What a form shows instead of its fields while the read that decides whether it can be used has not said yes: "loading", one line of why it
// cannot be used, or a failed read (the network, not an answer) with Try again. Shared by the Jira plan form and the Jira gap form.
import { Button, Notice } from "@graphico/ui";
import { V2_COPY } from "../v2-copy";
import { ConnectFrame } from "./ConnectFrame";

export type ReadGateState = "loading" | "failed" | "closed";

/** The gate state of a form whose own gate has more words: "loading" and "failed" are shown as such, anything else is a closed door. */
export const readGateState = (gate: string): ReadGateState => (gate === "failed" || gate === "loading" ? gate : "closed");

export function ReadGate({
  state,
  text,
  onBack,
  onRetry,
}: Readonly<{ state: ReadGateState; text: string; onBack: () => void; onRetry: () => void }>) {
  return (
    <ConnectFrame onBack={onBack} action={state === "failed" ? <Button onClick={onRetry}>{V2_COPY.tryAgain}</Button> : undefined}>
      <Notice role={state === "failed" ? "alert" : "status"}>{text}</Notice>
    </ConnectFrame>
  );
}
