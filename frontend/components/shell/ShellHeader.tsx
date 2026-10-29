"use client";
// FE-SHELL-02 (header row): one shared header row for every screen. The shell renders the TopBar once; its left slot (the
// design-system `children` lead, "page title or Breadcrumbs") is a portal target. A screen puts its tab bar there with
// <ShellHeader>, so the tabs and the global cluster sit on the same row, centred on the same axis, at the same place on every page.
// `tools` (filters, search, page actions) join the tabs on that row when it is wide enough and wrap, as one group, to a second
// line under the tabs when it is not (shell-gaps.css). The portal keeps the screen's React tree: state, context and handlers stay.
import { createContext, useContext, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { truthy } from "@/components/ui/utils";

interface Slot {
  target: HTMLElement | null;
  setTarget: (el: HTMLElement | null) => void;
}

const ShellHeaderContext = createContext<Slot | null>(null);

export function ShellHeaderProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [target, setTarget] = useState<HTMLElement | null>(null);
  return <ShellHeaderContext.Provider value={{ target, setTarget }}>{children}</ShellHeaderContext.Provider>;
}

/** The slot element, rendered by the shell inside the TopBar lead. */
export function ShellHeaderTarget() {
  const slot = useContext(ShellHeaderContext);
  return <div className="shell__head" data-testid="shell-head" ref={slot?.setTarget} />;
}

/** Renders the screen's tab bar (and optional tools) in the header row. Outside the shell (galleries, tests) it renders nothing. */
export function ShellHeader({ tabs, tools }: { tabs: ReactNode; tools?: ReactNode }) {
  const target = useContext(ShellHeaderContext)?.target;
  if (!target) return null;
  return createPortal(
    <>
      <div className="shell__line">{tabs}</div>
      {truthy(tools) ? <div className="shell__line shell__line--tools">{tools}</div> : null}
    </>,
    target,
  );
}
