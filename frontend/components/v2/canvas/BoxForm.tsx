"use client";
// Box link: one folder, file or shared link from Box, a check that lists the files an import would take (all checked; unchecking leaves a file out,
// like the Jira epic's issues and the Drive folder's files), then Add source (one source per link, D5; read once, D7). The acting account is the project's
// Box integration, so a project without it is sent to Integrations from the plus dialog ("Set up first"). A shared link is never stored
// (docs/box/source.md). The field is off while a call runs.
import { useId, useState } from "react";
import { Button, Field, Notice, PickList, PickRow, TextInput } from "@graphico/ui";
import type { BoxCheck } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { boxCheckText, boxFailureText, folderHint, idOfFile, selectionText } from "./box-logic";
import { checkAll, checkNone, excludeOf, pickedCount, toggle, type Unchecked } from "./pick-logic";
import { useCheckAdd } from "./use-check-add";

export function BoxForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [link, setLink] = useState("");
  const linkId = useId();
  const typed = link.trim();
  // The selection belongs to the typed link (like the check): another link starts again with every file checked.
  const [left, setLeft] = useState<{ signature: string; ids: Unchecked }>({ signature: "", ids: checkAll() });
  const unchecked = left.signature === typed ? left.ids : checkAll();
  const flow = useCheckAdd<BoxCheck>({
    signature: typed,
    blank: typed === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => sources.checkBox(projectId, typed),
    add: (found) => sources.addBox(projectId, typed, excludeOf(found.files.map(idOfFile), unchecked)),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: boxFailureText,
  });
  const files = flow.result?.files ?? [];
  const rows = files.map(idOfFile);
  const cappedNote = flow.result?.capped === true || flow.result?.filesCapped === true ? CANVAS_COPY.boxCapped : null;
  const nothingPicked = files.length > 0 && pickedCount(rows, unchecked) === 0;
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        if (!nothingPicked) flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || typed === "" || nothingPicked} aria-busy={flow.busy !== null || undefined}>
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
      {files.length === 0 && cappedNote !== null ? <Notice>{cappedNote}</Notice> : null}
      {files.length === 0 ? null : (
        <PickList
          legend={CANVAS_COPY.boxFiles}
          scroll
          notes={[selectionText(files, unchecked), cappedNote].filter((n) => n !== null)}
          tools={[
            {
              label: CANVAS_COPY.boxSelectAll,
              disabled: unchecked.size === 0 || flow.busy !== null,
              onClick: () => {
                setLeft({ signature: typed, ids: checkAll() });
              },
            },
            {
              label: CANVAS_COPY.boxSelectNone,
              disabled: unchecked.size === files.length || flow.busy !== null,
              onClick: () => {
                setLeft({ signature: typed, ids: checkNone(rows) });
              },
            },
          ]}
        >
          {files.map((file) => (
            <PickRow
              key={file.id}
              text
              label={file.name}
              hint={folderHint(file)}
              checked={!unchecked.has(file.id)}
              disabled={flow.busy !== null}
              onChange={(on) => {
                setLeft({ signature: typed, ids: toggle(unchecked, file.id, on) });
              }}
            />
          ))}
        </PickList>
      )}
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}
