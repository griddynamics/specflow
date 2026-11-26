"use client";
// Box link: one folder, file or shared link from Box, a check that counts what an import would take, then Add source (one source per link, D5; read
// once, D7). The acting account is the project's Box integration, so a project without it is sent to Integrations from the plus dialog ("Set up first").
// A shared link is never stored (docs/box/source.md). The field is off while a call runs.
import { useId, useState } from "react";
import { Button, Field, Notice, TextInput } from "@graphico/ui";
import type { BoxCheck } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { boxCheckText, boxFailureText } from "./box-logic";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { useCheckAdd } from "./use-check-add";

export function BoxForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [link, setLink] = useState("");
  const linkId = useId();
  const typed = link.trim();
  const flow = useCheckAdd<BoxCheck>({
    signature: typed,
    blank: typed === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => sources.checkBox(projectId, typed),
    add: () => sources.addBox(projectId, typed),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: boxFailureText,
  });
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || typed === ""} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.boxLink} htmlFor={linkId} hint={CANVAS_COPY.boxLinkHint}>
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
      <Notice>{CANVAS_COPY.boxNote}</Notice>
      <div role="status">{flow.result === null ? null : <Notice>{boxCheckText(flow.result)}</Notice>}</div>
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}
