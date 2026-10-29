"use client";
// FE-FILES-01, FE-DS-02, FE-DOD-05 (R5): port of `FileTree` in bundle.js. Markup and classes as the bundle (ul[role=tree] > li >
// button.gdg-tree__row). Addition (GAPS P05): WAI-ARIA tree keys with a roving tab stop: ArrowUp/Down/Home/End move, ArrowRight
// opens a folder or enters it, ArrowLeft closes it or goes to the parent; Enter and Space activate like a click.
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";
import { isFolder, treeKeyMove, visibleTreeRows, type ExpandedMap, type FileTreeNode } from "./app-logic";
import { useControlled } from "./use-controlled";
import { cx, nonEmpty, truthy } from "./utils";

export type { FileTreeNode };

export interface FileTreeProps {
  nodes: FileTreeNode[];
  defaultExpanded?: string[];
  selected?: string;
  defaultSelected?: string;
  onSelect?: (id: string) => void;
  title?: string;
  actions?: ReactNode;
  label?: string;
  className?: string;
}

function treeIcon(name: string, folder: boolean, open: boolean): IconName {
  if (folder) return open ? "folder-open" : "folder-simple";
  return /\.md$/i.test(name) ? "file-md" : "file-text";
}

export function FileTree({
  nodes,
  defaultExpanded,
  selected,
  defaultSelected,
  onSelect,
  title,
  actions,
  label,
  className,
}: Readonly<FileTreeProps>) {
  const [expanded, setExpanded] = useState<ExpandedMap>(() => Object.fromEntries((defaultExpanded ?? []).map((k) => [k, true])));
  const [sel, setSel] = useControlled<string | undefined>(selected, defaultSelected, (v) => v !== undefined && onSelect?.(v));
  const listRef = useRef<HTMLUListElement>(null);
  const rows = visibleTreeRows(nodes, expanded);
  // One tab stop: the selected file when it is visible, otherwise the first row.
  const tabId = rows.find((r) => r.node.id === sel)?.node.id ?? rows[0]?.node.id;

  const focusRow = (id: string) => listRef.current?.querySelector<HTMLElement>(`[data-node="${CSS.escape(id)}"]`)?.focus();
  function onKey(e: KeyboardEvent<HTMLElement>, id: string) {
    const move = treeKeyMove(e.key, rows, id, expanded);
    if (!move) return;
    e.preventDefault();
    if ("focus" in move) focusRow(move.focus);
    else setExpanded({ ...expanded, [move.toggle]: expanded[move.toggle] !== true });
  }

  function row(n: FileTreeNode, depth: number): ReactNode {
    const folder = isFolder(n);
    const open = expanded[n.id] === true;
    return (
      <li key={n.id} role="treeitem" aria-expanded={folder ? open : undefined} aria-selected={!folder ? sel === n.id : undefined}>
        <button
          type="button"
          data-node={n.id}
          tabIndex={n.id === tabId ? 0 : -1}
          className={cx("gdg-tree__row", sel === n.id && "is-on")}
          // 10px + 18px per level, as bundle.js, from tokens
          style={{ paddingLeft: `calc(var(--space-xs) + var(--space-2xs) / 2 + ${String(depth)} * (var(--space-sm) + var(--space-2xs)))` }}
          onClick={() => {
            if (folder) setExpanded({ ...expanded, [n.id]: !open });
            else setSel(n.id);
          }}
          onKeyDown={(e) => {
            onKey(e, n.id);
          }}
        >
          {folder ? (
            <Icon name={open ? "caret-down" : "caret-right"} size={12} className="gdg-tree__caret" />
          ) : (
            <span className="gdg-tree__caret" />
          )}
          <Icon name={treeIcon(n.name, folder, open)} size={18} />
          <span className="gdg-tree__name" title={n.name}>
            {n.name}
          </span>
          {n.badge != null ? <span className="gdg-tree__badge">{n.badge}</span> : null}
        </button>
        {folder && open ? <ul role="group">{(n.children ?? []).map((c) => row(c, depth + 1))}</ul> : null}
      </li>
    );
  }

  return (
    <div className={cx("gdg-tree", className)}>
      {truthy(title) || truthy(actions) ? (
        <div className="gdg-tree__head">
          <span>{title}</span>
          {actions}
        </div>
      ) : null}
      <ul role="tree" aria-label={nonEmpty(label) ?? "Files"} ref={listRef}>
        {nodes.map((n) => row(n, 0))}
      </ul>
    </div>
  );
}
