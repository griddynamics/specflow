"use client";
// FE-REV-03, FE-REV-04, FE-PPL-02.AC2, FE-PPL-03 (PLAN a.7, b.4): the right-hand review panel: one CommentThread for the open thread
// (or the draft of a new one), with the bot's typing row. `@` in the box suggests the mentionables (Alex, Nina, Sam,
// SpecBot). The panel only draws; FilesDocument owns the state and calls the comments port.
import { CommentThread } from "@/components/ui/CommentThread";
import { Icon } from "@/components/ui/Icon";
import type { DocRef } from "@/lib/domain/common";
import { docRefLink } from "@/lib/markdown/links";
import type { Thread } from "@/lib/domain/comments";
import type { Person } from "@/lib/domain/people";
import { baseName, commentRows } from "./files-logic";
import { useFilesDeps } from "./use-files-deps";

export type PanelTarget = { kind: "thread"; thread: Thread } | { kind: "draft"; quote: string };

/** The quoted text on top of the thread: the thread's anchor, or the draft's selection. */
function quoteOf(thread: Thread | null, target: PanelTarget): string | undefined {
  if (thread) return thread.anchor.quote;
  return target.kind === "draft" ? target.quote : undefined;
}

/** A source link's text: its label, or the file name (with the anchor) when it has none. */
function sourceLabel(ref: DocRef): string {
  if (ref.label !== undefined) return ref.label;
  const name = baseName(ref.path);
  return ref.anchor !== undefined && ref.anchor !== "" ? `${name}#${ref.anchor}` : name;
}

export function CommentPanel({
  target,
  people,
  typing,
  onSubmit,
  onResolve,
  onClose,
  projectId,
  onOpenRef,
}: Readonly<{
  target: PanelTarget;
  people: Person[];
  typing: boolean;
  onSubmit: (text: string) => void;
  onResolve: () => void;
  onClose: () => void;
  projectId: string;
  onOpenRef: (ref: DocRef) => void;
}>) {
  const { routes } = useFilesDeps();
  const thread = target.kind === "thread" ? target.thread : null;
  return (
    <aside className="files__panel" aria-label="Comments" data-testid="files-comments" data-thread={thread?.id ?? "draft"}>
      <div className="files__panel-head">
        <h3 className="files__panel-title title-3">Comments</h3>
        <button type="button" className="gdg-iconbtn" aria-label="Close comments" onClick={onClose}>
          <Icon name="x" size={16} />
        </button>
      </div>
      <CommentThread
        key={thread?.id ?? "draft"}
        quote={quoteOf(thread, target)}
        comments={thread ? commentRows(thread.comments, people, typing) : []}
        people={people}
        onSubmit={onSubmit}
        onResolve={thread?.status === "open" ? onResolve : undefined}
        resolved={thread?.status === "resolved"}
        autoFocus
      />
      {/* MOCKS §8.2 "Sources line": the bot's references, under the thread (CommentThread has no slot for them). */}
      {thread?.comments
        .filter((c) => c.kind === "bot" && (c.sources?.length ?? 0) > 0)
        .map((c) => (
          <nav key={c.id} className="files__sources" aria-label="Sources" data-testid="files-sources">
            <span className="files__sources-label">Sources</span>
            <ul>
              {(c.sources ?? []).map((ref) => (
                <li key={`${ref.path}#${ref.anchor ?? ""}`}>
                  <a
                    href={docRefLink(routes, projectId, ref)}
                    onClick={(e) => {
                      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                      e.preventDefault();
                      onOpenRef(ref);
                    }}
                  >
                    {sourceLabel(ref)}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        ))}
    </aside>
  );
}
