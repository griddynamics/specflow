"use client";
// FE-PLAN-07 (TECH-SPECS §2.15; DECISIONS AUTO-20 option B, P11-1, P24-1, P24-3, P27-3..5, P27-11, P27-12): the body of one task, built from the Files
// parts, imported and unchanged (`DocContext`, `BLOCK_COMPONENTS`, `selectedBlock`, `blockSource`, `headingIndex`, `CommentPanel`, `threadHighlights`,
// `useAskSpecBot`). The derived Markdown shows as formatted text. The selection toolbar offers Edit, Comment and Ask SpecBot as in Files (Q21).
// Edit: "Implements" and "Depends on" come from graph edges and refuse it; Save goes through `plan.updateTaskBody` with the task's `version` as
// `baseVersion`. Comment: the comments port with the path `plan:<taskId>` (never a Files path, so no Files view shows these threads); the thread
// panel opens to the right of the text, `@specbot` gets the scripted reply, highlights are drawn from the threads (re-read on mount, so they
// survive a tab switch). Ask SpecBot hands the exact selection to SpecBot. Comment and Ask SpecBot work on the graph lines too (P27-4).
// Esc in the editor cancels only the edit (the block editor handles it; nothing else here listens for Esc except the selection clear).
// The Markdown DOM is never touched by hand; the editor and the highlights are rendered by block-render.tsx from the context below.
import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useRouter } from "next/navigation";
import { useAskSpecBot } from "@/components/files/ask-specbot";
import { BLOCK_COMPONENTS, DocContext, type DocContextValue, type EditingBlock } from "@/components/files/block-render";
import { CommentPanel } from "@/components/files/CommentPanel";
import { selectedBlock } from "@/components/files/selection-dom";
import "@/components/files/files-gaps.css"; // the editor frame (`.files-edit*`, G-P11-4/5); imported, not edited (P24-1)
import { MarkdownProse } from "@/components/ui/MarkdownProse";
import { SelectionToolbar } from "@/components/ui/SelectionToolbar";
import { useToast } from "@/components/ui/Toast";
import { useTextSelection } from "@/components/ui/use-text-selection";
import { usePlanPort, usePlanRoutes } from "../use-plan-ports";
import type { DocRef } from "@/lib/domain/common";
import { isDataError } from "@/lib/domain/errors";
import type { PlanState, PlanTask } from "@/lib/domain/plan";
import { blockSource, buildAnchor, headingIndex } from "@/lib/markdown/blocks";
import { docRefLink } from "@/lib/markdown/links";
import { editorText, isGraphBlock, savedText, taskCommentPath } from "./task-logic";
import { useTaskThread } from "./use-task-thread";
import { tabStop, truthy } from "@/components/ui/utils";

// R24-03: under the page h1 "Plan" and the phase h2, a task's title is level 3 and its sections level 4 (and so on, capped at 6). The Files
// block parts keep their tags (`h1`..`h6`, source ranges, editor); the wrapper only adds role and aria-level, which win over the tag's level.
const HEADINGS = Object.fromEntries(
  (["h1", "h2", "h3", "h4", "h5", "h6"] as const).map((tag, i) => {
    const Inner = (BLOCK_COMPONENTS as Record<string, ComponentType<Record<string, unknown>>>)[tag];
    const Heading = (props: Record<string, unknown>) => <Inner {...props} role="heading" aria-level={Math.min(i + 3, 6)} />;
    Heading.displayName = `Task.${tag}`;
    return [tag, Heading];
  }),
);
const TASK_COMPONENTS = { ...BLOCK_COMPONENTS, ...HEADINGS };

export function TaskBody({ projectId, task, onSaved }: Readonly<{ projectId: string; task: PlanTask; onSaved: (task: PlanTask) => void }>) {
  const plan = usePlanPort();
  const routes = usePlanRoutes();
  const router = useRouter();
  const askSpecBot = useAskSpecBot(projectId);
  const toast = useToast();
  const paneRef = useRef<HTMLDivElement>(null);
  const [sel, clearSel] = useTextSelection(paneRef);
  const [editing, setEditing] = useState<EditingBlock | null>(null);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [note, setNote] = useState("");
  const [bar, setBar] = useState({ width: 0, room: 0 });
  const savingRef = useRef(false);
  const path = taskCommentPath(task.taskId);
  const { typing, panel, setPanel, mentionables, highlights, openThread, target, submit, resolve } = useTaskThread(projectId, path);

  const headings = useMemo(() => headingIndex(task.body), [task.body]);
  const focusPane = useCallback(() => paneRef.current?.focus({ preventScroll: true }), []);

  const close = useCallback(() => {
    setEditing(null);
    setEditError(null);
    setConflict(false);
  }, []);

  const save = useCallback(
    (text: string) => {
      if (!editing || savingRef.current) return; // a second Ctrl/Cmd+Enter while the first save runs does nothing
      if (text.trim() === "") {
        setEditError("A block cannot be empty. Cancel to keep the original text.");
        return;
      }
      savingRef.current = true;
      setSaving(true);
      setEditError(null);
      plan.updateTaskBody(projectId, task.taskId, editing, savedText(task.body, editing, text), task.version).then(
        (next) => {
          savingRef.current = false;
          setSaving(false);
          focusPane();
          close();
          setPanel((p) => (p?.kind === "draft" ? null : p)); // offsets of a draft are stale after an edit
          onSaved(next);
          toast({ title: "Task updated", tone: "success" });
        },
        (err: unknown) => {
          savingRef.current = false;
          setSaving(false);
          if (isDataError(err) && err.code === "conflict") {
            // keep the editor open with the message and a Reload action (what the user typed is not thrown away silently)
            setConflict(true);
            setEditError("This task changed while you were editing. Your change was not saved. Reload to see the latest text.");
            toast({ title: "Not saved", message: "The task changed.", tone: "danger" });
          } else if (isDataError(err) && err.code === "validation") {
            setEditError("This text cannot be saved. Keep one title line and do not add headings.");
          } else setEditError("Could not save. Try again.");
        },
      );
    },
    [editing, plan, projectId, task.taskId, task.body, task.version, onSaved, toast, focusPane, close, setPanel],
  );
  const cancel = useCallback(() => {
    focusPane();
    close();
  }, [focusPane, close]);
  const reload = useCallback(() => {
    plan.state(projectId).then(
      (s: PlanState) => {
        const next = s.phase === "ready" ? s.plan.tasks.find((t) => t.taskId === task.taskId) : undefined;
        if (!next) {
          setEditError("Could not reload the task.");
          return;
        }
        focusPane();
        close();
        onSaved(next);
      },
      () => {
        setEditError("Could not reload the task.");
      },
    );
  }, [plan, projectId, task.taskId, onSaved, focusPane, close]);

  // P26 m3: the title shows without its `# ` prefix and a plain title is accepted (`savedText` adds the prefix back).
  const editText = editing ? editorText(task.body, editing, blockSource(task.body, editing)) : "";
  const ctx = useMemo<DocContextValue>(
    () => ({
      body: task.body,
      headings,
      highlights,
      editing,
      editText,
      saving,
      editError,
      onSave: save,
      onCancel: cancel,
      onReload: conflict ? reload : null,
      onOpenThread: openThread,
    }),
    [task.body, headings, highlights, editing, editText, saving, editError, save, cancel, conflict, reload, openThread],
  );
  const element = useMemo(() => <MarkdownProse source={task.body} components={TASK_COMPONENTS} />, [task.body]);

  function onAction(id: string) {
    if (!sel || !paneRef.current) return;
    if (id === "ask") {
      askSpecBot(sel.text); // FE-PLAN-07.AC15: exactly the selected text, as in Files
      return;
    }
    const block = selectedBlock(sel.range, paneRef.current);
    if (id === "comment") {
      clearSel();
      if (!block?.range) {
        setNote("Select some text to comment on");
        return;
      }
      setNote("");
      setEditing(null);
      setPanel({
        kind: "draft",
        anchor: buildAnchor(path, block.section, block.text, block.range),
        blockId: block.blockId,
        ...block.range,
      });
      return;
    }
    if (id !== "edit") return;
    clearSel();
    if (!block?.edit) {
      setNote("Edit works on paragraphs, list items and headings");
      return;
    }
    if (isGraphBlock(task.body, block.edit)) {
      setNote("These lines come from the graph");
      return;
    }
    setNote("");
    setEditError(null);
    setConflict(false);
    setPanel((p) => (p?.kind === "draft" ? null : p));
    setEditing(block.edit);
  }

  // The toolbar sits inside the body frame; it stays inside it (the frame's visible width bounds its centre).
  const showing = !!sel && !editing;
  useLayoutEffect(() => {
    const pane = paneRef.current;
    if (!pane || !showing) return;
    const el = pane.querySelector<HTMLElement>(".gdg-seltool");
    setBar((cur) => {
      const next = { width: el ? el.offsetWidth : cur.width, room: pane.clientWidth };
      return cur.width === next.width && cur.room === next.room ? cur : next;
    });
  }, [showing, sel]);
  const shownNote = sel ? "" : note; // a new selection hides the old message (R24-04); the refusal clears the selection, so it shows
  const half = bar.width / 2;
  let barX = 0;
  if (sel) barX = bar.room > 0 ? Math.min(Math.max(sel.x, half), Math.max(half, bar.room - half)) : sel.x;

  // The text pane is a keyboard stop (Tab reaches it) and Esc drops the selection.
  const paneProps = {
    ...tabStop(true),
    onKeyDown: (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape" && sel) clearSel();
    },
  };

  return (
    <DocContext.Provider value={ctx}>
      <div className="plan-body-wrap" data-panel={target ? "" : undefined}>
        <div className="plan-body-main">
          <div
            ref={paneRef}
            className="plan-body"
            data-testid="plan-task-body"
            data-task={task.taskId}
            data-version={task.version}
            role="group"
            aria-label={`Task text: ${task.title}`}
            {...paneProps}
          >
            {element}
            {showing && truthy(sel) ? <SelectionToolbar x={barX} y={sel.y} onAction={onAction} /> : null}
          </div>
          <p className="plan-body__hint body-sm">Select text to edit, comment or ask SpecBot</p>
          {shownNote ? (
            <p className="plan-body__note body-sm" role="status" data-testid="plan-task-note">
              {shownNote}
            </p>
          ) : null}
        </div>
        {panel && target ? (
          <CommentPanel
            target={target}
            people={mentionables}
            typing={panel.kind === "thread" && typing.has(panel.id)}
            onSubmit={submit}
            onResolve={() => {
              if (panel.kind === "thread") resolve(panel.id);
            }}
            projectId={projectId}
            onOpenRef={(ref: DocRef) => {
              router.push(docRefLink(routes, projectId, ref));
            }}
            onClose={() => {
              const id = panel.kind === "thread" ? panel.id : null;
              setPanel(null);
              // focus returns to the highlight that opened the thread, else to the text (never to <body>)
              const mark = truthy(id) ? paneRef.current?.querySelector<HTMLElement>(`mark[data-thread="${CSS.escape(id)}"]`) : null;
              (mark ?? paneRef.current)?.focus({ preventScroll: true });
            }}
          />
        ) : null}
      </div>
    </DocContext.Provider>
  );
}
