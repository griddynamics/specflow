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

/** The longest link the API takes (`NotionLinkIn`). */
const LINK_MAX = 2048;

export function NotionForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [link, setLink] = useState("");
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
    <NotionFormView
      link={link}
      onLink={setLink}
      busy={flow.busy !== null}
      label={flow.label}
      result={flow.result}
      problem={flow.problem}
      onBack={onBack}
      onSubmit={flow.run}
    />
  );
}

/** The form as it looks for a state of the flow: the field is off while a call runs, the button says Check link, Checking… or Add source. */
export function NotionFormView(
  props: Readonly<{
    link: string;
    onLink: (link: string) => void;
    busy: boolean;
    label: string;
    result: NotionCheck | null;
    problem: string | null;
    onBack: () => void;
    onSubmit: () => void;
  }>,
) {
  const linkId = useId();
  const blank = props.link.trim() === "";
  return (
    <ConnectFrame
      onBack={props.onBack}
      onSubmit={(event) => {
        event.preventDefault();
        props.onSubmit();
      }}
      action={
        <Button type="submit" disabled={props.busy || blank} aria-busy={props.busy || undefined}>
          {props.label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.notionLink} htmlFor={linkId}>
        <TextInput
          id={linkId}
          value={props.link}
          spellCheck={false}
          maxLength={LINK_MAX}
          readOnly={props.busy}
          onChange={(event) => {
            props.onLink(event.target.value);
          }}
        />
      </Field>
      <Notice>{CANVAS_COPY.notionNote}</Notice>
      <NotionAnswer result={props.result} problem={props.problem} />
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
