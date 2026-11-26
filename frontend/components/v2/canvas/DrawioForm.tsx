"use client";
// The diagrams.net link: the address of a diagram (`#R` holds the diagram in the link, `#U` a public file), a check that counts what an import
// would take, then Add source (one source per link, read once, D7). The field is off while a call runs; changing the link asks for a new check.
import { useId, useState } from "react";
import { Button, Field, Notice, TextInput } from "@graphico/ui";
import type { DrawioCheck } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { HowTo } from "../HowTo";
import { useV2Ports } from "../use-v2-data";
import { ConnectFrame } from "./ConnectFrame";
import { DRAWIO_COPY, drawioCheckText, drawioFailureText } from "./drawio-logic";
import { useCheckAdd } from "./use-check-add";

export function DrawioForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [value, setValue] = useState("");
  const linkId = useId();
  const link = value.trim();
  const flow = useCheckAdd<DrawioCheck>({
    signature: link,
    blank: link === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => sources.checkDrawio(projectId, link),
    add: () => sources.addDrawio(projectId, link),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: (e) => drawioFailureText(e, link),
  });
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || link === ""} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={DRAWIO_COPY.link} htmlFor={linkId} error={flow.problem ?? undefined}>
        <TextInput
          id={linkId}
          type="text"
          inputMode="url"
          value={value}
          spellCheck={false}
          readOnly={flow.busy !== null}
          onChange={(event) => {
            setValue(event.target.value);
          }}
        />
      </Field>
      <HowTo steps={DRAWIO_COPY.how} />
      <div role="status">{flow.result === null ? null : <Notice>{drawioCheckText(flow.result)}</Notice>}</div>
    </ConnectFrame>
  );
}
