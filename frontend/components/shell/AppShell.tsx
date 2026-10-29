"use client";
// FE-SHELL-01, FE-SHELL-02, FE-DS-08, FE-DOD-06 (TECH-SPECS §1.8, §5.2, §5.4): the app shell. Sidebar, top bar,
// toasts, the create-project slot and the page column. Rendered by app/(app)/production-shell.tsx inside <DataProvider>.
//
// The only writer of `ui.currentProjectId` (§1.8): it sets the value from the `projectId` route param once `projects.get` has
// confirmed the project, and on a switch. An unknown or non-member id redirects to the default start point (PA27). Only a switch
// to a DIFFERENT project makes it current and opens its Mission Control (PA7, PA8).
import { useParams, usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, type ReactNode } from "react";
import { Sidebar, type SidebarEntry } from "@/components/ui/Sidebar";
import { Spinner } from "@/components/ui/ProgressBar";
import { ToastProvider } from "@/components/ui/Toast";
import { TopBar } from "@/components/ui/TopBar";
import type { MenuItem } from "@/components/ui/Menu";
import { CreateProjectSlot, CreateProjectSlotProvider, useCreateProjectSlot } from "./CreateProjectSlot";
import { ShellHeaderProvider, ShellHeaderTarget } from "./ShellHeader";
import { fallbackProjectId, sectionHref, sectionOf, SIDEBAR_SECTIONS, type SectionKey } from "./shell-logic";
import { useMe, useProjectGate, useProjectList, useShellEnv } from "./use-shell-data";
import "./shell-gaps.css";
import { truthy } from "@/components/ui/utils";
import { COPY } from "@/lib/copy";

/**
 * `accountItems` (P17, O12): the avatar menu items of the production tree ("Sign out"), passed by the production wrapper
 * (app/(app)/production-shell.tsx).
 */
export function AppShell({ children, accountItems }: Readonly<{ children: ReactNode; accountItems?: MenuItem[] }>) {
  return (
    <ToastProvider>
      <CreateProjectSlotProvider>
        <ShellHeaderProvider>
          <ShellFrame accountItems={accountItems}>{children}</ShellFrame>
        </ShellHeaderProvider>
      </CreateProjectSlotProvider>
    </ToastProvider>
  );
}

const SIDEBAR_ITEMS: SidebarEntry[] = SIDEBAR_SECTIONS.map((s) => ({ value: s.value, label: s.label, icon: s.icon }));

/** What the content region shows while the project in the path is being checked: the error note, or the spinner. */
function ProjectPending({ failed }: Readonly<{ failed: boolean }>) {
  if (failed) {
    return (
      <p className="body shell__note" role="alert" data-testid="shell-error">
        The project could not be loaded. Reload the page to try again.
      </p>
    );
  }
  return (
    <div className="shell__loading" data-testid="shell-loading">
      <Spinner size={24} label={COPY.loadingProject} />
    </div>
  );
}

function ShellFrame({ children, accountItems }: Readonly<{ children: ReactNode; accountItems?: MenuItem[] }>) {
  const { routes, current, setCurrent } = useShellEnv();
  const router = useRouter();
  const pathname = usePathname();
  const params = useParams<{ projectId?: string | string[] }>();
  const routeProject = Array.isArray(params.projectId) ? params.projectId[0] : params.projectId;
  const slot = useCreateProjectSlot();
  const me = useMe();
  const list = useProjectList();

  const mainRef = useRef<HTMLElement>(null);

  // §1.8, PA27: unknown or inaccessible project -> default start point, no page, no toast.
  const gate = useProjectGate(routeProject, () => {
    router.replace(routes.home());
  });
  const confirmed = routeProject !== undefined && gate?.id === routeProject && gate.status === "ok";

  // The setter changes identity on every render; the effects below only care about their inputs.
  const setCurrentRef = useRef(setCurrent);
  const currentRef = useRef(current);
  useEffect(() => {
    setCurrentRef.current = setCurrent;
    currentRef.current = current;
  });

  // Writer 1: the route, once per route id (after the gate confirmed it). A change of `current` alone (the switcher's own write,
  // made before the new route lands) must never be undone by the old route, so the effect acts only when the route id changes.
  const lastRouteWritten = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!confirmed || lastRouteWritten.current === routeProject) return;
    lastRouteWritten.current = routeProject;
    if (routeProject !== currentRef.current) setCurrentRef.current(routeProject); // `current` is read only when the route id changes
  }, [confirmed, routeProject]);

  // Writer 2: no project in the path (Connections) and none stored -> the default (§1.8).
  useEffect(() => {
    if (routeProject !== undefined || current !== null || list === null) return;
    const fallback = fallbackProjectId(list.map((p) => p.id));
    if (truthy(fallback)) setCurrentRef.current(fallback);
  }, [routeProject, current, list]);

  const activeProject: string | null = current ?? (routeProject !== undefined && confirmed ? routeProject : null);

  function onSection(value: string) {
    if (!truthy(activeProject) && value !== "connections") return; // list not loaded yet: the link would lead to the entry route
    router.push(sectionHref(routes, value as SectionKey, activeProject));
  }

  // Switcher: a DIFFERENT project becomes current and opens its Mission Control (§5.2).
  function onProjectChange(id: string) {
    if (current === id) return; // picking the current project does nothing (PA8)
    setCurrent(id);
    router.push(routes.missionControl(id));
  }

  // FE-BP-03.AC3 (O12): the avatar menu items ("Sign out") come from the production wrapper.
  const userItems: MenuItem[] = accountItems ?? [];

  const switcher = (list ?? []).map((p) => ({ id: p.id, name: p.name }));
  const awaiting = routeProject !== undefined && !confirmed;

  return (
    <div className="shell" data-testid="app-shell">
      <div className="shell__nav">
        <Sidebar
          items={SIDEBAR_ITEMS}
          value={sectionOf(pathname) ?? ""}
          onChange={onSection}
          defaultCollapsed
          logoSrc={routes.asset("Logos/full-color-black-horizontal.svg")}
          logoDarkSrc={routes.asset("Logos/full-color-white-horizontal.svg")}
          markSrc={routes.asset("Logos/full-color-black-mark.svg")}
        />
      </div>
      <div className="shell__col">
        {/* The header row: the screen's tab bar in the left slot (ShellHeader) and the global cluster. Both branches are the same
            TopBar in the same place, so React keeps the element and the slot (with what a screen put in it) when the data arrives.
            Until then the cluster is hidden but keeps its size (GAPS G-P07-6); the row height is fixed in shell-gaps.css. */}
        {me && list ? (
          <TopBar
            projects={switcher}
            projectId={activeProject ?? undefined}
            onProjectChange={onProjectChange}
            onNew={slot.open}
            newInProjectMenu // owner decision 2026-10-15: creating a project lives in the project dropdown
            newLabel="Create project"
            user={{ name: me.name, email: me.email }}
            userItems={userItems}
          >
            <ShellHeaderTarget />
          </TopBar>
        ) : (
          <TopBar className="shell__topbar--ph" projects={[]} newInProjectMenu user={{ name: "Loading" }} userItems={[]}>
            <ShellHeaderTarget />
          </TopBar>
        )}
        <main className="shell__main" data-testid="shell-main" ref={mainRef} tabIndex={-1}>
          {awaiting ? <ProjectPending failed={gate?.id === routeProject && gate.status === "error"} /> : children}
        </main>
      </div>
      <CreateProjectSlot open={slot.isOpen} onClose={slot.close} />
    </div>
  );
}
