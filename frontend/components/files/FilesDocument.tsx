"use client";
// FE-MC-08, FE-REV-01..04, FE-PPL-02.AC2, FE-PPL-03 (PLAN a.7): the reading pane of the Files view for ONE document and the review
// on top of it. Mounted with `key={path}` by FilesView, so edit mode, the draft and the open thread start clean for every file
// (FE-DEMO-08.AC6); threads, versions and text live in the store and come back through the ports.
//
// State kept here: threads of the file, the bot typing set, the open panel (thread or draft with its block id and offsets), the block
// in edit mode. The Markdown DOM is never changed by hand: highlights and the editor are rendered by block-render.tsx from this state.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { MarkdownProse } from "@/components/ui/MarkdownProse";
import { SelectionToolbar } from "@/components/ui/SelectionToolbar";
import { useTextSelection } from "@/components/ui/use-text-selection";
import { useToast } from "@/components/ui/Toast";
import type { DocRef } from "@/lib/domain/common";
import type { TextAnchor, Thread } from "@/lib/domain/comments";
import { isDataError } from "@/lib/domain/errors";
import type { ProjectDocument } from "@/lib/domain/files";
import type { Person } from "@/lib/domain/people";
import { blockSource, buildAnchor, headingIndex } from "@/lib/markdown/blocks";
import { docLinkComponents } from "@/lib/markdown/doc-links";
import { scrollToSection } from "@/lib/markdown/scroll";
import { BLOCK_COMPONENTS, DocContext, type DocContextValue, type EditingBlock } from "./block-render";
import { CommentPanel, type PanelTarget } from "./CommentPanel";
import { threadHighlights, type Highlight } from "./files-logic";
import { selectedBlock } from "./selection-dom";
import { useFilesDeps } from "./use-files-deps";
import { useStableCallback } from "./use-stable-callback";

type Panel = { kind: "thread"; id: string } | { kind: "draft"; anchor: TextAnchor; blockId: number; start: number; end: number };

/** The toolbar's horizontal position: the selection's x, kept inside the visible width when the pane has one. */
function toolbarX(sel: { x: number } | null, room: number, half: number): number {
  if (!sel) return 0;
  return room > 0 ? Math.min(Math.max(sel.x, half), Math.max(half, room - half)) : sel.x;
}

/** What the comment panel shows: the draft's quote, or the open thread when it still exists. */
function panelTarget(panel: Panel | null, current: Thread | undefined): PanelTarget | null {
  if (panel?.kind === "draft") return { kind: "draft", quote: panel.anchor.quote };
  return panel && current ? { kind: "thread", thread: current } : null;
}

/** The threads with `created` added, unless it is already there. */
function withThread(threads: Thread[], created: Thread): Thread[] {
  return threads.some((t) => t.id === created.id) ? threads : [...threads, created];
}

export function FilesDocument({
  projectId,
  doc,
  section,
  scrollNonce,
  people,
  isKnown,
  onOpenRef,
  onAskSpecBot,
  onDocChange,
}: Readonly<{
  projectId: string;
  doc: ProjectDocument;
  section: string | null;
  /** Changes when a link to the same file and section is clicked again: scroll once more. */
  scrollNonce: number;
  people: Person[];
  isKnown: (path: string) => boolean;
  onOpenRef: (ref: DocRef) => void;
  onAskSpecBot: (selectedText: string) => void;
  onDocChange: (doc: ProjectDocument) => void;
}>) {
  const { files, comments, routes } = useFilesDeps();
  const toast = useToast();
  const paneRef = useRef<HTMLDivElement>(null);
  const [sel, clearSel] = useTextSelection(paneRef);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [typing, setTyping] = useState<ReadonlySet<string>>(new Set());
  const [panel, setPanel] = useState<Panel | null>(null);
  const [editing, setEditing] = useState<EditingBlock | null>(null);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  // Toolbar geometry: its width, the pane's visible width and visible vertical range. Measured when a selection appears and updated on
  // scroll ONLY while a selection is shown, so plain scrolling re-renders nothing.
  const [bar, setBar] = useState({ width: 0, room: 0, top: 0, height: Number.POSITIVE_INFINITY });
  const savingRef = useRef(false);
  const [conflict, setConflict] = useState(false);
  const path = doc.path;

  // Threads of this file; bot events keep them current (FE-REV-04).
  const reload = useCallback(
    () =>
      comments.list(projectId, path).then(
        (list) => {
          setThreads(list);
        },
        () => {
          setThreads([]);
        },
      ),
    [comments, projectId, path],
  );
  useEffect(() => {
    let alive = true;
    comments.list(projectId, path).then(
      (list) => {
        if (alive) setThreads(list);
      },
      () => {
        if (alive) setThreads([]);
      },
    );
    const off = comments.subscribe(projectId, (e) => {
      if (e.type === "thread.changed") reload();
      else
        setTyping((cur) => {
          const next = new Set(cur);
          if (e.typing) next.add(e.threadId);
          else next.delete(e.threadId);
          return next;
        });
    });
    return () => {
      alive = false;
      off();
    };
  }, [comments, projectId, path, reload]);

  // Deep link: scroll the section to the top of the pane and focus its heading (FE-MC-08.AC5).
  useEffect(() => {
    if (section !== null && section !== "" && paneRef.current) scrollToSection(section, paneRef.current);
  }, [path, section, scrollNonce]);

  const headings = useMemo(() => headingIndex(doc.body), [doc.body]);
  // The handlers read the latest callbacks at click time; the object itself must keep its identity (a new `a` component type
  // would remount the whole document), so the callbacks are wrapped in stable functions.
  const openLatest = useStableCallback(onOpenRef);
  const knownLatest = useStableCallback(isKnown);
  const linkComponents = useMemo(
    () =>
      docLinkComponents({
        currentPath: path,
        projectId,
        routes,
        onOpen: openLatest,
        isKnown: knownLatest,
      }),
    [path, projectId, routes, openLatest, knownLatest],
  );
  const components = useMemo(() => ({ ...BLOCK_COMPONENTS, ...linkComponents }), [linkComponents]);

  const highlights = useMemo<Highlight[]>(() => {
    const list = threadHighlights(threads);
    if (panel?.kind === "draft") list.push({ key: "draft", threadId: null, blockId: panel.blockId, start: panel.start, end: panel.end });
    return list;
  }, [threads, panel]);

  const focusPane = useCallback(() => paneRef.current?.focus({ preventScroll: true }), []);
  const leaveEditor = useCallback(() => {
    focusPane();
    setEditing(null);
  }, [focusPane]);
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
      files.updateBlock(projectId, path, editing, text, doc.version).then(
        (next) => {
          savingRef.current = false;
          setSaving(false);
          leaveEditor();
          setPanel((p) => (p?.kind === "draft" ? null : p)); // offsets of a draft are stale after an edit
          onDocChange(next);
          toast({ title: "Changes saved", tone: "success" });
        },
        (err: unknown) => {
          savingRef.current = false;
          setSaving(false);
          if (isDataError(err) && err.code === "conflict") {
            // keep the editor open with the message and a Reload action (nothing the user typed is thrown away silently)
            setConflict(true);
            setEditError("This document changed while you were editing. Your change was not saved. Reload to see the latest text.");
            toast({ title: "Not saved", message: "The document changed.", tone: "danger" });
          } else setEditError("Could not save. Try again.");
        },
      );
    },
    [editing, files, projectId, path, doc.version, onDocChange, toast, leaveEditor],
  );
  const cancel = useCallback(() => {
    leaveEditor();
    setEditError(null);
    setConflict(false);
  }, [leaveEditor]);
  const reloadDoc = useCallback(() => {
    files.get(projectId, path).then(
      (next) => {
        cancel();
        onDocChange(next);
      },
      () => {
        setEditError("Could not reload the file.");
      },
    );
  }, [files, projectId, path, onDocChange, cancel]);
  const openThread = useCallback((id: string) => {
    setPanel({ kind: "thread", id });
  }, []);

  const editText = editing ? blockSource(doc.body, editing) : "";
  const ctx = useMemo<DocContextValue>(
    () => ({
      body: doc.body,
      headings,
      highlights,
      editing,
      editText,
      saving,
      editError,
      onSave: save,
      onCancel: cancel,
      onReload: conflict ? reloadDoc : null,
      onOpenThread: openThread,
    }),
    [doc.body, headings, highlights, editing, editText, saving, editError, save, cancel, conflict, reloadDoc, openThread],
  );

  const element = useMemo(() => <MarkdownProse source={doc.body} paper components={components} />, [doc.body, components]);

  function onAction(id: string) {
    if (!sel || !paneRef.current) return;
    if (id === "ask") {
      onAskSpecBot(sel.text);
      return;
    }
    const block = selectedBlock(sel.range, paneRef.current);
    if (id === "edit") {
      if (!block?.edit) {
        toast({ title: "Edit works on paragraphs, list items and headings", tone: "info" });
        return;
      }
      setEditError(null);
      setConflict(false);
      setEditing(block.edit);
      setPanel((p) => (p?.kind === "draft" ? null : p));
      clearSel();
      return;
    }
    if (id === "comment") {
      if (!block?.range) {
        toast({ title: "Select some text to comment on", tone: "info" });
        return;
      }
      setEditing(null);
      setPanel({
        kind: "draft",
        anchor: buildAnchor(path, block.section, block.text, block.range),
        blockId: block.blockId,
        ...block.range,
      });
      clearSel();
    }
  }

  const current = panel?.kind === "thread" ? threads.find((t) => t.id === panel.id) : undefined;
  // The toolbar moves with the text (it lives inside the scrolling pane); it hides once the selection has left the visible part.
  const toolbarVisible = !!sel && !editing && sel.y - bar.top >= 0 && sel.y - bar.top <= bar.height;
  const showing = !!sel && !editing;
  useLayoutEffect(() => {
    const pane = paneRef.current;
    if (!pane || !showing) return;
    const el = pane.querySelector<HTMLElement>(".gdg-seltool");
    // The toolbar may not be in the DOM yet on the pass where `top`/`height` were stale (it renders only once they are fresh), so
    // the effect runs again when `toolbarVisible` flips; a missing toolbar keeps the last measured width instead of storing 0.
    setBar((cur) => {
      const next = { width: el ? el.offsetWidth : cur.width, room: pane.clientWidth, top: pane.scrollTop, height: pane.clientHeight };
      return cur.width === next.width && cur.room === next.room && cur.top === next.top && cur.height === next.height ? cur : next;
    });
  }, [showing, sel, toolbarVisible]);
  // Escape in the pane drops the selection (and with it the toolbar).
  useEffect(() => {
    const pane = paneRef.current;
    if (!pane || !sel) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") clearSel();
    };
    pane.addEventListener("keydown", onKey);
    return () => {
      pane.removeEventListener("keydown", onKey);
    };
  }, [sel, clearSel]);
  const half = bar.width / 2;
  const barX = toolbarX(sel, bar.room, half);
  const target = panelTarget(panel, current);

  function submit(text: string) {
    if (!panel) return;
    const done = (id: string, created?: Thread) => {
      if (created) setThreads((cur) => withThread(cur, created));
      setPanel({ kind: "thread", id });
      reload();
      toast({ title: "Comment added", tone: "success" });
    };
    const fail = () => {
      toast({ title: "Comment not sent", message: "Try again.", tone: "danger" });
    };
    if (panel.kind === "draft")
      comments.create(projectId, panel.anchor, text).then((t) => {
        done(t.id, t);
      }, fail);
    else
      comments.reply(projectId, panel.id, text).then(() => {
        done(panel.id);
      }, fail);
  }

  return (
    <DocContext.Provider value={ctx}>
      <div className="files__doc" data-testid="files-doc" data-path={path} data-version={doc.version}>
        <div
          ref={paneRef}
          className="files__pane"
          data-testid="files-pane"
          role="region"
          aria-label="Document"
          tabIndex={0}
          onScroll={(e) => {
            if (!showing) return;
            const { scrollTop, clientHeight } = e.currentTarget;
            setBar((cur) => ({ ...cur, top: scrollTop, height: clientHeight }));
          }}
        >
          {element}
          {toolbarVisible ? <SelectionToolbar x={barX} y={sel.y} onAction={onAction} /> : null}
        </div>
        {panel && target ? (
          <CommentPanel
            target={target}
            people={people}
            typing={panel.kind === "thread" && typing.has(panel.id)}
            onSubmit={submit}
            onResolve={() => {
              if (panel.kind === "thread") comments.resolve(projectId, panel.id).then(() => reload());
            }}
            projectId={projectId}
            onOpenRef={onOpenRef}
            onClose={() => {
              const id = panel.kind === "thread" ? panel.id : null;
              setPanel(null);
              // focus returns to the highlight that opened the thread, else to the pane (never to <body>)
              const mark = id !== null ? paneRef.current?.querySelector<HTMLElement>(`mark[data-thread="${CSS.escape(id)}"]`) : null;
              (mark ?? paneRef.current)?.focus({ preventScroll: true });
            }}
          />
        ) : null}
      </div>
    </DocContext.Provider>
  );
}
