"use client";
// FE-MC-08, FE-REV-01..04, FE-PPL-02.AC2, FE-PPL-03 (PLAN a.7, b.4): the Files view of Mission Control Ready (hand-over from P09: the
// export name and the props `{ projectId, file, section }` are unchanged; `file` and `section` come from the deep link
// `?view=files&file=&section=` of TECH-SPECS §1.4).
//
//  - Left: FileTree (`files.tree`) with the "Add files" button. Right: breadcrumbs and the reading pane (FilesDocument) with the
//    selection toolbar, edit, comments and the review panel.
//  - The URL is the state: picking a file or following a document link pushes `routes.file(...)` with `history.pushState`, the same
//    way the view switcher does (P09); the parent re-reads the query and passes new `file` / `section` props.
//  - PA9: no file in the URL -> the first file in tree order. An unknown path -> "Select a file".
//  - Add files (O8, PA21): the native picker; the files appear under "Uploads", no processing, memory only.
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type RefObject } from "react";
import { Breadcrumbs } from "@/components/ui/Breadcrumbs";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { FileTree } from "@/components/ui/FileTree";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Icon } from "@/components/ui/Icon";
import { Spinner } from "@/components/ui/ProgressBar";
import { useToast } from "@/components/ui/Toast";
import { useNotAvailable } from "@/components/shell/use-not-available";
import type { DocRef } from "@/lib/domain/common";
import type { FileTreeNode, ProjectDocument } from "@/lib/domain/files";
import type { Person } from "@/lib/domain/people";
import { filesLink } from "@/lib/markdown/links";
import { useAskSpecBot } from "./ask-specbot";
import { FilesDocument } from "./FilesDocument";
import { filePaths, firstFilePath, folderIds, nodeChain } from "./files-logic";
import { useFilesDeps } from "./use-files-deps";
import "./files-gaps.css";
import { COPY } from "@/lib/copy";

type DocState = { path: string; doc: ProjectDocument } | { path: string; failed: true } | null;
type TreeState = FileTreeNode[] | "failed" | null;
type FilesDeps = ReturnType<typeof useFilesDeps>;

/** MOCKS §10.2 (Files view, an uploaded file selected). */
const UPLOAD_NOTE = {
  title: "Uploaded contents are not shown here yet.",
  body: 'The file is listed under "Uploads".',
};

/** Tree, project name and the "@" list (people.mentionables, MOCKS §8.3). */
function useFilesViewData(deps: FilesDeps, projectId: string) {
  const { files, projects, people } = deps;
  const [tree, setTree] = useState<TreeState>(null);
  const [projectName, setProjectName] = useState("Project");
  const [mentionables, setMentionables] = useState<Person[]>([]);
  useEffect(() => {
    let alive = true;
    const loadTree = () =>
      files.tree(projectId).then(
        (t) => {
          if (alive) setTree(t);
        },
        () => {
          if (alive) setTree("failed");
        },
      );
    loadTree();
    projects.get(projectId).then(
      (p) => {
        if (alive) setProjectName(p.name);
      },
      () => undefined,
    );
    people.mentionables(projectId).then(
      (list) => {
        if (alive) setMentionables(list);
      },
      () => undefined,
    );
    const off = files.subscribe(projectId, () => {
      void loadTree();
    });
    return () => {
      alive = false;
      off();
    };
  }, [files, projects, people, projectId]);
  return { tree, projectName, mentionables };
}

/** The selected document, loaded when the effective path changes. */
function useSelectedDocument(files: FilesDeps["files"], projectId: string, effective: string | null) {
  const [docState, setDocState] = useState<DocState>(null);
  useEffect(() => {
    if (effective === null) return;
    let alive = true;
    files.get(projectId, effective).then(
      (doc) => {
        if (alive) setDocState({ path: effective, doc });
      },
      () => {
        if (alive) setDocState({ path: effective, failed: true });
      },
    );
    return () => {
      alive = false;
    };
  }, [files, projectId, effective]);
  return [docState, setDocState] as const;
}

export function FilesView({
  projectId,
  file,
  section,
  onAskSpecBot,
  treeless,
}: Readonly<{
  projectId: string;
  file: string | null;
  section: string | null;
  /** Handler for the toolbar's "Ask SpecBot" with the exact selected text (P13). Default: `useAskSpecBot` in ./ask-specbot.ts. */
  onAskSpecBot?: (selectedText: string) => void;
  /** Phase 1 Mission Control sources: one document opened from a link, without the file tree and "Add files". Default false. */
  treeless?: boolean;
}>) {
  const deps = useFilesDeps();
  const { files, routes } = deps;
  const toast = useToast();
  const notAvailable = useNotAvailable();
  const defaultAsk = useAskSpecBot(projectId);
  const { tree, projectName, mentionables } = useFilesViewData(deps, projectId);
  const [scrollNonce, setScrollNonce] = useState(0);
  const picker = useRef<HTMLInputElement>(null);
  const { box: treeBox, focus: focusTreeRow } = useTreeFocus();

  const nodes = Array.isArray(tree) ? tree : null;
  const known = useMemo(() => new Set(nodes ? filePaths(nodes) : []), [nodes]);
  const path = file ?? (nodes ? firstFilePath(nodes) : null);
  const effective = path !== null && known.has(path) ? path : null;
  const [docState, setDocState] = useSelectedDocument(files, projectId, effective);

  const pushFile = useCallback(
    (ref: DocRef) => {
      const href = filesLink(routes, projectId, ref.path, ref.anchor);
      const now = window.location.pathname + window.location.search;
      if (href === now) return false;
      window.history.pushState(null, "", href);
      return true;
    },
    [routes, projectId],
  );
  const openRef = useCallback(
    (ref: DocRef) => {
      // Same URL already (same file, same section): nothing changes in the props, so scroll once more by hand. A new URL scrolls by itself.
      if (!pushFile(ref) && ref.path === effective) setScrollNonce((n) => n + 1);
    },
    [pushFile, effective],
  );
  const onSelect = useCallback(
    (id: string) => {
      const chain = nodes ? findById(nodes, id) : null;
      if (chain?.path !== undefined && chain.path !== "") pushFile({ path: chain.path });
    },
    [nodes, pushFile],
  );
  const onPick = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const list = Array.from(e.target.files ?? []);
      e.target.value = ""; // the same file can be picked again
      if (list.length === 0) return;
      files.addFiles(projectId, list).then(
        () => undefined,
        (err: unknown) => {
          if (!notAvailable.from(err)) toast({ title: "Files not added", message: "Try again.", tone: "danger" });
        },
      );
    },
    [files, projectId, notAvailable, toast],
  );

  const chain = nodes && effective !== null ? nodeChain(nodes, effective) : null;
  const crumbs = chain
    ? [
        {
          label: projectName,
          onClick: () => {
            focusTreeRow(null);
          },
        },
        ...chain.map((n) => ({
          label: n.name,
          onClick: () => {
            focusTreeRow(n.id);
          },
        })),
      ]
    : [];
  const ask = onAskSpecBot ?? defaultAsk;
  const current = docState !== null && effective !== null && docState.path === effective ? docState : null;

  const selectedId = nodes && effective !== null ? (chain?.[chain.length - 1]?.id ?? undefined) : undefined;
  return (
    <section
      className={treeless === true ? "files files--treeless" : "files"}
      data-testid="view-files"
      data-project={projectId}
      data-file={file ?? effective ?? undefined}
      data-section={section ?? undefined}
    >
      {treeless === true ? null : (
        <FilesTreePane
          tree={tree}
          nodes={nodes}
          selectedId={selectedId}
          pickerRef={picker}
          boxRef={treeBox}
          onSelect={onSelect}
          onPick={onPick}
        />
      )}
      <div className="files__main">
        {crumbs.length > 0 ? <Breadcrumbs items={crumbs} label="File path" /> : null}
        <div className="files__body">
          <FilesBody
            projectId={projectId}
            tree={tree}
            nodes={nodes}
            effective={effective}
            current={current}
            section={section}
            scrollNonce={scrollNonce}
            people={mentionables}
            known={known}
            onOpenRef={openRef}
            onAskSpecBot={ask}
            onDocChange={(next) => {
              setDocState({ path: next.path, doc: next });
            }}
          />
        </div>
      </div>
    </section>
  );
}

/** The left column: the tree and the "Add files" button. */
function FilesTreePane({
  tree,
  nodes,
  selectedId,
  pickerRef,
  boxRef,
  onSelect,
  onPick,
}: Readonly<{
  tree: TreeState;
  nodes: FileTreeNode[] | null;
  selectedId: string | undefined;
  pickerRef: RefObject<HTMLInputElement | null>;
  boxRef: RefObject<HTMLDivElement | null>;
  onSelect: (id: string) => void;
  onPick: (e: ChangeEvent<HTMLInputElement>) => void;
}>) {
  let treeBody = null;
  if (nodes) {
    treeBody = (
      <FileTree
        key={folderIds(nodes).join("|")}
        nodes={nodes}
        defaultExpanded={folderIds(nodes)}
        selected={selectedId}
        onSelect={onSelect}
        label="Project files"
      />
    );
  } else if (tree !== "failed") {
    treeBody = <Spinner label={COPY.loadingFiles} />;
  }
  return (
    <GlassSurface variant="strong" className="files__tree" as="aside" aria-label="Project files">
      <div className="gdg-tree__head files__tree-head">
        <span>Files</span>
        <Button variant="glass" size="sm" onClick={() => pickerRef.current?.click()}>
          <Icon name="plus" size={14} />
          Add files
        </Button>
        <input ref={pickerRef} type="file" multiple hidden data-testid="files-add-input" aria-label="Add files" onChange={onPick} />
      </div>
      <div ref={boxRef} className="files__tree-body" data-testid="files-tree">
        {treeBody}
      </div>
    </GlassSurface>
  );
}

/** The right column's content: a state message, or the document. */
function FilesBody({
  projectId,
  tree,
  nodes,
  effective,
  current,
  section,
  scrollNonce,
  people,
  known,
  onOpenRef,
  onAskSpecBot,
  onDocChange,
}: Readonly<{
  projectId: string;
  tree: TreeState;
  nodes: FileTreeNode[] | null;
  effective: string | null;
  current: Exclude<DocState, null> | null;
  section: string | null;
  scrollNonce: number;
  people: Person[];
  known: ReadonlySet<string>;
  onOpenRef: (ref: DocRef) => void;
  onAskSpecBot: (selectedText: string) => void;
  onDocChange: (doc: ProjectDocument) => void;
}>) {
  if (tree === "failed") {
    return (
      <EmptyState title="Files are not available yet" icon="folder-simple">
        Upload files in Mission Control and create the project to see its documents here.
      </EmptyState>
    );
  }
  if (!nodes || (effective !== null && !current)) return <Spinner label={COPY.loadingDocument} />;
  if (effective === null) {
    return (
      <EmptyState title="Select a file" icon="file-md">
        Choose a document in the tree to read it here.
      </EmptyState>
    );
  }
  if (current && "failed" in current) {
    return (
      <EmptyState title="This file could not be opened" icon="file-md">
        Choose another document in the tree.
      </EmptyState>
    );
  }
  if (current?.doc.uploaded) {
    return (
      <div className="files__note" data-testid="files-upload-note">
        <EmptyState title={UPLOAD_NOTE.title} icon="file-text">
          {UPLOAD_NOTE.body}
        </EmptyState>
      </div>
    );
  }
  if (current && "doc" in current) {
    return (
      <FilesDocument
        key={current.doc.path}
        projectId={projectId}
        doc={current.doc}
        section={section}
        scrollNonce={scrollNonce}
        people={people}
        isKnown={(p) => known.has(p)}
        onOpenRef={onOpenRef}
        onAskSpecBot={onAskSpecBot}
        onDocChange={onDocChange}
      />
    );
  }
  return null;
}

function findById(nodes: FileTreeNode[], id: string): FileTreeNode | null {
  for (const n of nodes) {
    if (n.id === id) return n;
    const inner = n.children ? findById(n.children, id) : null;
    if (inner) return inner;
  }
  return null;
}

/** The tree's box and the breadcrumb click: moves focus to the tree row of that folder (or the first row for the project). */
function useTreeFocus() {
  const box = useRef<HTMLDivElement>(null);
  const focus = useCallback((id: string | null) => {
    focusRow(box.current, id);
  }, []);
  return { box, focus };
}

function focusRow(box: HTMLElement | null, id: string | null) {
  const el =
    id === null ? box?.querySelector<HTMLElement>("[data-node]") : box?.querySelector<HTMLElement>(`[data-node="${CSS.escape(id)}"]`);
  el?.focus();
}
