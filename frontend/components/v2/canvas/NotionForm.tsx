"use client";
// Notion pages: the pages and databases the member the project's Notion is connected by shared with Graphico (`sources.listNotion`), as a
// checklist like the GitHub repositories and the Box files. What is picked is added as one source (docs/notion/source.md "Picking a page") and
// imported once (D7): each page with its sub-pages, each database with its rows, a page picked with its parent read once. The acting account is
// the project's Notion integration, so a project without it is sent to Integrations from the plus dialog ("Set up first"); a binding lost since
// then comes back from the list or the add as a 409 and is said in words. A refused add adds nothing and reads the list again. The list is off
// while the add runs.
import { useState } from "react";
import { Button, Notice, PickList, PickRow } from "@graphico/ui";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { NOTION_PICKER_WORDS, notionAddFailureText, notionPickedText } from "./notion-logic";
import { type NotionSharedRead, useNotionShared } from "./NotionPicker";

export function NotionForm({ projectId, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [round, setRound] = useState(0);
  const shared = useNotionShared(projectId, round);
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const add = async (links: readonly string[]) => {
    setBusy(true);
    setProblem(null);
    try {
      await sources.addNotion(projectId, links);
      reload.current();
      onClose();
    } catch (e) {
      setBusy(false);
      setRound((n) => n + 1);
      setProblem(notionAddFailureText(e));
    }
  };

  return (
    <NotionFormView
      shared={shared}
      picked={picked}
      busy={busy}
      problem={problem}
      onPick={setPicked}
      onBack={onBack}
      onAdd={(links) => void add(links)}
    />
  );
}

/** The form for a read of the shared list: the checklist (pages first, then databases), Select all and Select none, and Add selected. */
export function NotionFormView(
  props: Readonly<{
    shared: NotionSharedRead;
    picked: ReadonlySet<string>;
    busy: boolean;
    problem: string | null;
    onPick: (picked: ReadonlySet<string>) => void;
    onBack: () => void;
    /** The links picked that are still in the list, in its order. */
    onAdd: (links: readonly string[]) => void;
  }>,
) {
  const { shared, picked, busy } = props;
  const all = shared.kind === "ready" ? shared.shared.items : [];
  const items = [...all.filter((i) => i.kind === "page"), ...all.filter((i) => i.kind === "database")];
  const chosen = items.filter((i) => picked.has(i.link)).map((i) => i.link);
  const count = chosen.length;
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
            props.onAdd(chosen);
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
