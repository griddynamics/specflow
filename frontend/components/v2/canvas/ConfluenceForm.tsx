"use client";
// Confluence link: the link of a page or a space of the project's Atlassian site and "Follow links", a check that counts what an import would
// take, then Add source (one source per link, D5; read once, D7). The acting account is the project's Atlassian integration, so a project
// without it is sent to Integrations from the plus dialog ("Set up first"). The fields are off while a call runs.
import { useId, useState } from "react";
import { Button, Field, Notice, PickRow, TextInput } from "@graphico/ui";
import type { ConfluenceCheck } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { confluenceCheckText, confluenceFailureText } from "./confluence-logic";
import { ConnectFrame } from "./ConnectFrame";
import { useCheckAdd } from "./use-check-add";

export function ConfluenceForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [link, setLink] = useState("");
  const [follow, setFollow] = useState(true);
  const linkId = useId();
  const input = { link: link.trim(), follow };
  const flow = useCheckAdd<ConfluenceCheck>({
    signature: `${input.link}|${String(follow)}`,
    blank: input.link === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => sources.checkConfluence(projectId, input),
    add: () => sources.addConfluence(projectId, input),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: confluenceFailureText,
  });
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || input.link === ""} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.confluenceLink} htmlFor={linkId}>
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
      <PickRow
        label={CANVAS_COPY.confluenceFollow}
        checked={follow}
        disabled={flow.busy !== null}
        onChange={(on) => {
          setFollow(on);
        }}
      />
      <Notice>{CANVAS_COPY.confluenceNote}</Notice>
      <div role="status">{flow.result === null ? null : <Notice>{confluenceCheckText(flow.result, follow)}</Notice>}</div>
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}
