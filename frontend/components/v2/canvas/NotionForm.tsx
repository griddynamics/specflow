"use client";
// Notion pages: the pages and databases the member the project's Notion is connected by shared with Graphico (`sources.listNotion`), as a
// checklist like the GitHub repositories and the Box files. Each one picked is added as its own source (one source per page or database, D5)
// and imported once (D7); a page brings its sub-pages, a database its rows. The acting account is the project's Notion integration, so a project
// without it is sent to Integrations from the plus dialog ("Set up first"); a binding lost since then comes back from the list or the add as a
// 409 and is said in words. The list is off while the adds run.
import { useState } from "react";
import { Button, Notice, PickList, PickRow } from "@graphico/ui";
import type { NotionSharedItem } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { NOTION_PICKER_WORDS, notionAddFailureText, notionPickedText } from "./notion-logic";
import { type NotionSharedRead, useNotionShared } from "./NotionPicker";

export function NotionForm({ projectId, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const shared = useNotionShared(projectId);
  const [added, setAdded] = useState<ReadonlySet<string>>(new Set());
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const add = async (items: readonly NotionSharedItem[]) => {
    setBusy(true);
    setProblem(null);
    const done = new Set<string>();
    let current: NotionSharedItem | null = null;
    try {
      for (const item of items.filter((i) => picked.has(i.link))) {
        current = item;
        await sources.addNotion(projectId, item.link);
        done.add(item.link);
      }
      reload.current();
      onClose();
    } catch (e) {
      // Some may be in: read the canvas again, and leave only the ones that are not.
      reload.current();
      setBusy(false);
      setAdded((now) => new Set([...now, ...done]));
      setPicked((now) => new Set([...now].filter((link) => !done.has(link))));
      setProblem(current === null ? V2_COPY.actionFailed : notionAddFailureText(current.title, e));
    }
  };

  return (
    <NotionFormView
      shared={shared}
      added={added}
      picked={picked}
      busy={busy}
      problem={problem}
      onPick={setPicked}
      onBack={onBack}
      onAdd={(items) => void add(items)}
    />
  );
}

/** The form for a read of the shared list: the checklist (pages first, then databases), Select all and Select none, and Add selected. */
export function NotionFormView(
  props: Readonly<{
    shared: NotionSharedRead;
    /** Links added by an earlier click of this form (a failed batch): they leave the list. */
    added: ReadonlySet<string>;
    picked: ReadonlySet<string>;
    busy: boolean;
    problem: string | null;
    onPick: (picked: ReadonlySet<string>) => void;
    onBack: () => void;
    onAdd: (items: readonly NotionSharedItem[]) => void;
  }>,
) {
  const { shared, picked, busy } = props;
  const all = shared.kind === "ready" ? shared.shared.items.filter((i) => !props.added.has(i.link)) : [];
  const items = [...all.filter((i) => i.kind === "page"), ...all.filter((i) => i.kind === "database")];
  const count = items.filter((i) => picked.has(i.link)).length;
  let body;
  if (shared.kind === "loading") body = <Notice>{NOTION_PICKER_WORDS.loading}</Notice>;
  else if (shared.kind === "failed") body = <Notice role="alert">{shared.text}</Notice>;
  else if (items.length === 0) body = <Notice>{NOTION_PICKER_WORDS.nothing}</Notice>;
  else {
    body = (
      <PickList
        legend={NOTION_PICKER_WORDS.legend}
        scroll
        notes={[notionPickedText(count), ...(shared.shared.capped ? [NOTION_PICKER_WORDS.capped] : [])]}
        tools={[
          {
            label: CANVAS_COPY.jiraEpicSelectAll,
            disabled: busy || count === items.length,
            onClick: () => {
              props.onPick(new Set(items.map((i) => i.link)));
            },
          },
          {
            label: CANVAS_COPY.jiraEpicSelectNone,
            disabled: busy || count === 0,
            onClick: () => {
              props.onPick(new Set());
            },
          },
        ]}
      >
        {items.map((item) => (
          <PickRow
            key={item.link}
            text
            label={item.title}
            hint={item.parentTitle === null ? undefined : `in ${item.parentTitle}`}
            note={item.kind === "database" ? NOTION_PICKER_WORDS.database : undefined}
            checked={picked.has(item.link)}
            disabled={busy}
            onChange={(on) => {
              props.onPick(on ? new Set([...picked, item.link]) : new Set([...picked].filter((l) => l !== item.link)));
            }}
          />
        ))}
      </PickList>
    );
  }
  return (
    <ConnectFrame
      onBack={props.onBack}
      action={
        <Button
          disabled={busy || count === 0}
          aria-busy={busy || undefined}
          onClick={() => {
            props.onAdd(items);
          }}
        >
          {busy ? CANVAS_COPY.githubAdding : CANVAS_COPY.githubAdd}
        </Button>
      }
    >
      {body}
      <Notice>{CANVAS_COPY.notionNote}</Notice>
      {props.problem === null ? null : <Notice role="alert">{props.problem}</Notice>}
    </ConnectFrame>
  );
}
