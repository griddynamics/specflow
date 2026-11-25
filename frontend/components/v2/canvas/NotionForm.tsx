"use client";
// Notion link: the link of a page or a database the member shared with Graphico, a check that counts what an import would take, then Add source
// (one source per link, D5; read once, D7). The acting account is the project's Notion integration, so a project without it is sent to
// Integrations from the plus dialog ("Set up first"); a binding lost since then comes back from the check or the add as a 409 and is said in
// words under the field. The field is off while a call runs.
import { useId, useState } from "react";
import { Button, Field, Notice, TextInput } from "@graphico/ui";
import type { NotionCheck } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { notionCheckText, notionFailureText } from "./notion-logic";
import { useCheckAdd } from "./use-check-add";

export function NotionForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [link, setLink] = useState("");
  const linkId = useId();
  const trimmed = link.trim();
  const flow = useCheckAdd<NotionCheck>({
    signature: trimmed,
    blank: trimmed === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => sources.checkNotion(projectId, trimmed),
    add: () => sources.addNotion(projectId, trimmed),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: notionFailureText,
  });
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || trimmed === ""} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.notionLink} htmlFor={linkId}>
        <TextInput
          id={linkId}
          value={link}
          spellCheck={false}
          readOnly={flow.busy !== null}
          onChange={(event) => {
            setLink(event.target.value);
          }}
        />
      </Field>
      <Notice>{CANVAS_COPY.notionNote}</Notice>
      <NotionAnswer result={flow.result} problem={flow.problem} />
    </ConnectFrame>
  );
}

/** What the form says after a call: what a passed check would import, or the refusal in words. */
export function NotionAnswer({ result, problem }: Readonly<{ result: NotionCheck | null; problem: string | null }>) {
  return (
    <>
      <div role="status">{result === null ? null : <Notice>{notionCheckText(result)}</Notice>}</div>
      {problem === null ? null : <Notice role="alert">{problem}</Notice>}
    </>
  );
}
