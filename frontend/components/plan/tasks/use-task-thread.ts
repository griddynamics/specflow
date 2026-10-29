"use client";
// FE-PLAN-07.AC11-AC14 (DECISIONS P27-3, P27-11): the comment state of ONE task body, split from TaskBody.tsx. Threads of the path `plan:<taskId>`
// (read again on every mount, so highlights survive a tab switch or a visit to another screen), the bot's typing set, the open panel (a thread or the
// draft of a new one), the "@" list, the highlights (Files `threadHighlights`) and the send and resolve calls. State only; the DOM stays in TaskBody.
import { useCallback, useEffect, useMemo, useState } from "react";
import type { PanelTarget } from "@/components/files/CommentPanel";
import { threadHighlights, type Highlight } from "@/components/files/files-logic";
import { useToast } from "@/components/ui/Toast";
import { usePorts } from "@/lib/data/provider";
import type { TextAnchor, Thread } from "@/lib/domain/comments";
import type { Person } from "@/lib/domain/people";

export type Panel = { kind: "thread"; id: string } | { kind: "draft"; anchor: TextAnchor; blockId: number; start: number; end: number };

/** The threads with `created` added once (a refresh may have brought it in already). */
function withThread(threads: Thread[], created: Thread): Thread[] {
  return threads.some((t) => t.id === created.id) ? threads : [...threads, created];
}

export function useTaskThread(projectId: string, path: string) {
  const { comments, people } = usePorts();
  const toast = useToast();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [typing, setTyping] = useState<ReadonlySet<string>>(new Set());
  const [panel, setPanel] = useState<Panel | null>(null);
  const [mentionables, setMentionables] = useState<Person[]>([]);

  const reloadThreads = useCallback(
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
    people.mentionables(projectId).then(
      (list) => {
        if (alive) setMentionables(list);
      },
      () => undefined,
    );
    const off = comments.subscribe(projectId, (e) => {
      if (!alive) return;
      if (e.type === "thread.changed") void reloadThreads();
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
  }, [comments, people, projectId, path, reloadThreads]);

  const highlights = useMemo<Highlight[]>(() => {
    const list = threadHighlights(threads);
    if (panel?.kind === "draft") list.push({ key: "draft", threadId: null, blockId: panel.blockId, start: panel.start, end: panel.end });
    return list;
  }, [threads, panel]);
  const openThread = useCallback((id: string) => {
    setPanel({ kind: "thread", id });
  }, []);

  const current = panel?.kind === "thread" ? threads.find((t) => t.id === panel.id) : undefined;
  let target: PanelTarget | null = null;
  if (panel?.kind === "draft") target = { kind: "draft", quote: panel.anchor.quote };
  else if (panel && current) target = { kind: "thread", thread: current };

  const done = useCallback(
    (id: string, created?: Thread) => {
      if (created) setThreads((cur) => withThread(cur, created));
      setPanel({ kind: "thread", id });
      void reloadThreads();
      toast({ title: "Comment added", tone: "success" });
    },
    [reloadThreads, toast],
  );
  const submit = useCallback(
    (text: string) => {
      if (!panel) return;
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
    },
    [panel, comments, projectId, done, toast],
  );
  const resolve = useCallback(
    (id: string) => void comments.resolve(projectId, id).then(() => reloadThreads()),
    [comments, projectId, reloadThreads],
  );

  return { threads, typing, panel, setPanel, mentionables, highlights, openThread, target, submit, resolve };
}
