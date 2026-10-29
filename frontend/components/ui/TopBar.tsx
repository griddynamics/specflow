"use client";
// FE-SHELL-02, FE-DS-02 (R5): port of `TopBar` in bundle.js. Right-aligned glass cluster: project switcher (Menu), New button,
// avatar menu. `defaultProjectMenuOpen` and `defaultUserMenuOpen` are in bundle.js and its preview, not in index.d.ts.
// Addition (owner decision 2026-10-15): `newInProjectMenu` puts "New project" at the end of the project switcher's list instead of the
// separate yellow New button. The app shell sets it; without it the component renders exactly as bundle.js (galleries, parity tests).
import type { ComponentPropsWithRef, ReactNode } from "react";
import { Avatar } from "./Avatar";
import { Button } from "./Button";
import { GlassSurface } from "./GlassSurface";
import { Icon } from "./Icon";
import { Menu, type MenuItem } from "./Menu";
import { cx, nonEmpty, truthy } from "./utils";

/** The title (`<h1>`) of a screen: the one primitive that renders it, so every screen keeps the same page-title role. */
export function PageTitle(props: ComponentPropsWithRef<"h1">) {
  return <h1 {...props} />;
}

export interface TopBarProps {
  projects: { id: string; name: string; meta?: string }[];
  projectId?: string;
  onProjectChange?: (id: string) => void;
  onNew?: () => void;
  newLabel?: string;
  /** "New project" as the last item of the project list, not a separate button (needs `onNew`). */
  newInProjectMenu?: boolean;
  user: { name: string; email?: string };
  /** Defaults to Settings, Reset demo, Sign out (all without handlers: the caller passes its own). */
  userItems?: MenuItem[];
  defaultProjectMenuOpen?: boolean;
  defaultUserMenuOpen?: boolean;
  className?: string;
  /** Left side: page title or Breadcrumbs. */
  children?: ReactNode;
}

const DEFAULT_USER_ITEMS: MenuItem[] = [
  { divider: true },
  { label: "Settings", icon: "gear-six" },
  { label: "Reset demo", icon: "arrow-counter-clockwise" },
  { divider: true },
  { label: "Sign out", icon: "sign-out" },
];

export function TopBar({
  projects,
  projectId,
  onProjectChange,
  onNew,
  newLabel,
  newInProjectMenu,
  user,
  userItems,
  defaultProjectMenuOpen,
  defaultUserMenuOpen,
  className,
  children,
}: Readonly<TopBarProps>) {
  const cur = projects.find((x) => x.id === projectId) ?? projects.at(0) ?? { id: "", name: "No project" };
  const projectItems: MenuItem[] = projects.map((x) => ({
    label: x.name,
    description: x.meta,
    checked: x.id === cur.id,
    onSelect: () => onProjectChange?.(x.id),
  }));
  if (truthy(newInProjectMenu) && onNew)
    projectItems.push({ divider: true }, { label: nonEmpty(newLabel) ?? "New project", icon: "plus", onSelect: onNew });
  return (
    <header className={cx("gdg-topbar", className)}>
      <div className="gdg-topbar__lead">{children}</div>
      <GlassSurface className="gdg-topbar__cluster" floating data-testid="topbar-cluster">
        <Menu
          label="Switch project"
          align="end"
          defaultOpen={defaultProjectMenuOpen}
          triggerClassName="gdg-topbar__project"
          trigger={
            <>
              <Icon name="folder-simple" size={18} />
              <span className="gdg-topbar__pname" data-testid="topbar-project-name">
                {cur.name}
              </span>
            </>
          }
          header={<div className="gdg-menu__section">Projects</div>}
          items={projectItems}
        />
        {truthy(newInProjectMenu) ? null : (
          <>
            <span className="gdg-topbar__sep" aria-hidden />
            <Button variant="primary" size="sm" onClick={onNew} className="gdg-topbar__new" data-testid="topbar-new">
              <Icon name="plus" size={14} />
              {nonEmpty(newLabel) ?? "New"}
            </Button>
          </>
        )}
        <Menu
          label="Account"
          align="end"
          defaultOpen={defaultUserMenuOpen}
          trigger={(t) => (
            <button
              type="button"
              className="gdg-topbar__user"
              aria-haspopup="menu"
              aria-expanded={t.open}
              aria-controls={t.open ? t.id : undefined}
              aria-label={`Account: ${user.name}`}
              onClick={t.toggle}
            >
              <Avatar name={user.name} size="md" title={false} />
            </button>
          )}
          header={
            <div className="gdg-menu__who">
              <Avatar name={user.name} size="lg" />
              <span>
                <b>{user.name}</b>
                <small>{user.email}</small>
              </span>
            </div>
          }
          items={userItems ?? DEFAULT_USER_ITEMS}
        />
      </GlassSurface>
    </header>
  );
}
