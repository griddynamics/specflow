"use client";
// Linear project or issue: a project or issue link, or an identifier (`GRA-12`), a check that lists the issues, then Add source (a source per
// add; what is read is read once, D7). Same flow and same acting account as the Jira epic: the member the project's Linear is connected by.
import { useId, useState } from "react";
import { Button, Field, Notice, PickList, PickRow, TextInput } from "@graphico/ui";
import type { LinearSourceCheck } from "@/lib/domain/linear-source";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import {
  idOfItem,
  LINEAR_SOURCE_COPY,
  linearAddable,
  linearSelectionText,
  linearSourceCheckText,
  linearSourceFailureText,
} from "./linear-source-logic";
import { checkAll, checkNone, excludeOf, toggle, type Unchecked } from "./pick-logic";
import { ConnectFrame } from "./ConnectFrame";
import { useCheckAdd } from "./use-check-add";

export function LinearSourceForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { linearSource } = useV2Ports();
  const [link, setLink] = useState("");
  const linkId = useId();
  const typed = link.trim();
  // The selection belongs to the typed link (like the check): another link starts again with every issue checked.
  const [left, setLeft] = useState<{ signature: string; ids: Unchecked }>({ signature: "", ids: checkAll() });
  const unchecked = left.signature === typed ? left.ids : checkAll();
  const flow = useCheckAdd<LinearSourceCheck>({
    signature: typed,
    blank: typed === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => linearSource.check(projectId, typed),
    add: (found) => linearSource.add(projectId, typed, excludeOf(found.items.map(idOfItem), unchecked)),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: linearSourceFailureText,
  });
  const found = flow.result;
  const items = found?.items ?? [];
  const rows = items.map(idOfItem);
  const nothingToAdd = found !== null && !linearAddable(found, unchecked);
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || typed === "" || nothingToAdd} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={LINEAR_SOURCE_COPY.field} htmlFor={linkId} hint={LINEAR_SOURCE_COPY.hint}>
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
      <div role="status">{found === null ? null : <Notice>{linearSourceCheckText(found)}</Notice>}</div>
      {found === null || items.length === 0 ? null : (
        <PickList
          legend={LINEAR_SOURCE_COPY.issues}
          scroll
          notes={[linearSelectionText(found, unchecked), ...(found.capped ? [LINEAR_SOURCE_COPY.capped] : [])]}
          tools={[
            {
              label: LINEAR_SOURCE_COPY.selectAll,
              disabled: unchecked.size === 0 || flow.busy !== null,
              onClick: () => {
                setLeft({ signature: typed, ids: checkAll() });
              },
            },
            {
              label: LINEAR_SOURCE_COPY.selectNone,
              disabled: unchecked.size === items.length || flow.busy !== null,
              onClick: () => {
                setLeft({ signature: typed, ids: checkNone(rows) });
              },
            },
          ]}
        >
          {items.map((item) => (
            <PickRow
              key={item.identifier}
              text
              code={item.identifier}
              label={item.title}
              note={item.parent === null ? item.stateName : `${item.stateName} · ${item.parent}`}
              checked={!unchecked.has(item.identifier)}
              disabled={flow.busy !== null}
              onChange={(on) => {
                setLeft({ signature: typed, ids: toggle(unchecked, item.identifier, on) });
              }}
            />
          ))}
        </PickList>
      )}
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}
