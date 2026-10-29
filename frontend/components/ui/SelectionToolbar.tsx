"use client";
// FE-FILES-03, FE-REV-01, FE-DS-02 (R5): port of `SelectionToolbar` in bundle.js. Floating glass toolbar above selected text.
// mousedown is cancelled (preventDefault) so pressing a button does not collapse the selection.
import type { CSSProperties } from "react";
import { GlassSurface } from "./GlassSurface";
import { Icon, type IconName } from "./Icon";
import { cx, truthy } from "./utils";

export interface SelectionToolbarAction {
  id: string;
  label: string;
  icon: IconName;
  accent?: boolean;
}

export interface SelectionToolbarProps {
  /** Centre x and top y inside the positioned container (from useTextSelection). Omit to render inline. */
  x?: number;
  y?: number;
  onAction?: (id: string) => void;
  actions?: SelectionToolbarAction[];
}

export const DEFAULT_SELECTION_ACTIONS: SelectionToolbarAction[] = [
  { id: "edit", label: "Edit", icon: "pencil-simple" },
  { id: "comment", label: "Comment", icon: "chat-circle-text" },
  { id: "ask", label: "Ask SpecBot", icon: "sparkle", accent: true },
];

export function SelectionToolbar({ x, y, onAction, actions = DEFAULT_SELECTION_ACTIONS }: Readonly<SelectionToolbarProps>) {
  // Position is computed by the caller; `translate` (not `transform`) centres the toolbar 10px above y, because the `gdg-pop`
  // entrance animation sets `transform` and would drop the offset for its 140 ms (bundle.js has that glitch).
  const style: CSSProperties | undefined =
    x != null
      ? { position: "absolute", left: x, top: y, translate: "-50% calc(-100% - var(--space-xs) - var(--space-2xs) / 2)" }
      : undefined;
  return (
    <GlassSurface
      variant="strong"
      floating
      className="gdg-seltool"
      role="toolbar"
      aria-label="Selection actions"
      style={style}
      onMouseDown={(e) => {
        e.preventDefault();
      }}
    >
      {actions.map((a) => (
        <button
          key={a.id}
          type="button"
          className={cx("gdg-seltool__btn", truthy(a.accent) && "is-accent")}
          onClick={() => onAction?.(a.id)}
        >
          <Icon name={a.icon} size={16} />
          {a.label}
        </button>
      ))}
    </GlassSurface>
  );
}
