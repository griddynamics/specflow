"use client";
// The form of the Linear gap channel (stream S5). FOUNDATION STUB: one line saying it cannot be used yet, inside the connect frame; the stream replaces
// the body with its fields, check and add (see GitHubPlanForm for the pattern) and keeps this export.
import type { FormProps } from "../connector-types";
import { V2_COPY } from "../v2-copy";
import { ReadGate } from "./ReadGate";

const noop = (): void => undefined;

export function LinearGapForm(props: Readonly<FormProps>) {
  return <ReadGate state="closed" text={V2_COPY.linearGapsUnavailable} onBack={props.onBack} onRetry={noop} />;
}
