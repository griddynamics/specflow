"use client";
// What the member the project's Notion is connected by shared with Graphico on Notion's consent page (docs/notion/source.md "Picking a page"),
// read once when a form opens, so nobody types or pastes a link: `useNotionShared` feeds the source checklist (`NotionForm`) and the parent page
// picker of the plan destination (`NotionPickerView`, one page: its database is created under it). Nothing shared, a refused read and a capped
// list are said in words under the picker.
import { useEffect, useId, useState } from "react";
import { Field, Notice, Select } from "@graphico/ui";
import type { NotionShared } from "@/lib/domain/sources";
import { useV2Ports } from "../use-v2-data";
import { NOTION_PICKER_WORDS, notionFailureText, notionOptionLabel } from "./notion-logic";

export type NotionSharedRead = { kind: "loading" } | { kind: "failed"; text: string } | { kind: "ready"; shared: NotionShared };

/** What `sources.listNotion` says for the project: the shared pages and databases, or why they cannot be read. A new `round` reads it again. */
export function useNotionShared(projectId: string, round = 0): NotionSharedRead {
  const { sources } = useV2Ports();
  const [read, setRead] = useState<NotionSharedRead>({ kind: "loading" });
  useEffect(() => {
    let alive = true;
    sources.listNotion(projectId).then(
      (shared) => {
        if (alive) setRead({ kind: "ready", shared });
      },
      (e: unknown) => {
        if (alive) setRead({ kind: "failed", text: notionFailureText(e) });
      },
    );
    return () => {
      alive = false;
    };
  }, [sources, projectId, round]);
  return read;
}

/** The parent page picker of the plan destination: the shared pages (never a database), one to pick. `value` is the link of the page picked. */
export function NotionPickerView(
  props: Readonly<{
    read: NotionSharedRead;
    label: string;
    hint?: string;
    value: string;
    disabled: boolean;
    onChange: (link: string) => void;
  }>,
) {
  const fieldId = useId();
  const { read } = props;
  const pages = read.kind === "ready" ? read.shared.items.filter((i) => i.kind === "page") : [];
  let placeholder: string = NOTION_PICKER_WORDS.pick;
  if (read.kind === "loading") placeholder = NOTION_PICKER_WORDS.loading;
  else if (pages.length === 0) placeholder = NOTION_PICKER_WORDS.none;
  return (
    <>
      <Field label={props.label} htmlFor={fieldId} hint={props.hint}>
        <Select
          id={fieldId}
          value={props.value}
          disabled={props.disabled || pages.length === 0}
          onChange={(event) => {
            props.onChange(event.target.value);
          }}
        >
          <option value="">{placeholder}</option>
          {pages.map((page) => (
            <option key={page.link} value={page.link}>
              {notionOptionLabel(page)}
            </option>
          ))}
        </Select>
      </Field>
      {read.kind === "failed" ? <Notice role="alert">{read.text}</Notice> : null}
      {read.kind === "ready" && pages.length === 0 ? <Notice>{NOTION_PICKER_WORDS.noPages}</Notice> : null}
      {read.kind === "ready" && read.shared.capped ? <Notice>{NOTION_PICKER_WORDS.capped}</Notice> : null}
    </>
  );
}
