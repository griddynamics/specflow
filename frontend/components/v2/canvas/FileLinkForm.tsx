"use client";
// A file link (Box, Dropbox): one folder, file or shared link, a check that lists the files an import would take (all checked; unchecking leaves a file
// out, like the Jira epic's issues and the Drive folder's files), then Add source (one source per link, D5; read once, D7). The acting account is the
// project's integration of the app, so a project without it is sent to Integrations from the plus dialog ("Set up first"). A shared link is never stored.
// The field is off while a call runs. The list words are the Box form's, which every file link shares.
import { useId, useState } from "react";
import { Button, Field, Notice, PickList, PickRow, TextInput } from "@graphico/ui";
import type { FormProps } from "../connector-types";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { boxCheckText, folderHint, idOfFile, selectionText, type ListedCheck } from "./box-logic";
import { checkAll, checkNone, excludeOf, pickedCount, toggle, type Unchecked } from "./pick-logic";
import { useCheckAdd } from "./use-check-add";

/** What one app's link form brings: its field words, its check and add calls (as the bound member) and the words of its closed failure codes. */
export interface FileLinkApp<C extends ListedCheck> {
  copy: { link: string; linkHint: string; note: string };
  check: (projectId: string, link: string) => Promise<C>;
  add: (projectId: string, link: string, exclude: readonly string[]) => Promise<void>;
  failureText: (e: unknown) => string;
}

export function FileLinkForm<C extends ListedCheck>({
  app,
  projectId,
  spec,
  reload,
  onBack,
  onClose,
}: Readonly<FormProps & { app: FileLinkApp<C> }>) {
  const [link, setLink] = useState("");
  const linkId = useId();
  const typed = link.trim();
  // The selection belongs to the typed link (like the check): another link starts again with every file checked.
  const [left, setLeft] = useState<{ signature: string; ids: Unchecked }>({ signature: "", ids: checkAll() });
  const unchecked = left.signature === typed ? left.ids : checkAll();
  const flow = useCheckAdd<C>({
    signature: typed,
    blank: typed === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => app.check(projectId, typed),
    add: (found) => app.add(projectId, typed, excludeOf(found.files.map(idOfFile), unchecked)),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: app.failureText,
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
      <Field label={app.copy.link} htmlFor={linkId} hint={app.copy.linkHint}>
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
      <Notice>{app.copy.note}</Notice>
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
