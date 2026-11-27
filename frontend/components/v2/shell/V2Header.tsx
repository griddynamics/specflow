"use client";
// D11: the V2 header. Brand, the project menu (the projects and "Create new project"), then Mission Control, Members, Integrations, the
// theme switch and the avatar with Sign out. A person with no project sees the brand, the theme switch and the avatar only. No heading
// in the body repeats what the header says (owner preference).
import { useRouter } from "next/navigation";
import type { MouseEvent } from "react";
import { AppHeader, Avatar, Brand, Menu, NavButton, NavLink, ThemeToggle, type MenuItem } from "@graphico/ui";
import type { Person } from "@/lib/domain/people";
import type { Project } from "@/lib/domain/projects";
import type { RouteBuilder } from "@/lib/routes";
import { V2_COPY } from "../v2-copy";
import { useTheme } from "./use-theme";

/**
 * The header's navigation, in order (D11): the project menu (the projects and "Create new project"), then Mission Control (the canvas),
 * the Members dialog and Integrations. The same tree is declared in `validation/nav-spec.json`, which the nav-tree check compares this
 * array to; the entries that name a route carry its `ROUTES` key.
 */
const HEADER_NAV: readonly { readonly key: string; readonly label: string }[] = [
  { key: "projects", label: V2_COPY.projectsMenu },
  { key: "canvas", label: V2_COPY.missionControl },
  { key: "members", label: V2_COPY.members },
  { key: "integrations", label: V2_COPY.integrations },
];

export interface V2HeaderProps {
  routes: RouteBuilder;
  pathname: string;
  projects: readonly Project[] | null;
  projectId: string | null;
  me: Person | null;
  onCreate: () => void;
  onMembers: () => void;
  onSignOut: () => void;
}

export function V2Header({ routes, pathname, projects, projectId, me, onCreate, onMembers, onSignOut }: Readonly<V2HeaderProps>) {
  const router = useRouter();
  const { theme, toggle } = useTheme();
  const current = projects?.find((p) => p.id === projectId);
  const [projectMenu, ...nav] = HEADER_NAV;
  const projectItems: MenuItem[] = (projects ?? []).map((p) => ({
    key: p.id,
    label: p.name,
    current: p.id === projectId,
    onSelect: () => {
      if (p.id !== projectId) router.push(routes.canvas(p.id));
    },
  }));
  const items: MenuItem[] = [
    ...projectItems,
    { key: "create", label: V2_COPY.createProject, separatorBefore: projectItems.length > 0, onSelect: onCreate },
  ];
  const go = (href: string) => (e: MouseEvent) => {
    e.preventDefault();
    router.push(href);
  };
  const canvas = projectId === null ? null : routes.canvas(projectId);
  const integrations = projectId === null ? null : routes.integrations(projectId);
  return (
    <AppHeader
      navLabel={V2_COPY.navLabel}
      brand={<Brand href={routes.home()} name={V2_COPY.brandName} />}
      project={
        projects === null || projects.length === 0 ? undefined : (
          <Menu look="project" label={projectMenu.label} trigger={current?.name ?? projectMenu.label} items={items} />
        )
      }
    >
      {canvas === null || integrations === null ? null : (
        <>
          {nav.map((item) => {
            if (item.key === "members") {
              return (
                <NavButton key={item.key} opensDialog onClick={onMembers}>
                  {item.label}
                </NavButton>
              );
            }
            const href = item.key === "canvas" ? canvas : integrations;
            return (
              <NavLink key={item.key} href={href} current={pathname === href} onNavigate={go(href)}>
                {item.label}
              </NavLink>
            );
          })}
        </>
      )}
      <ThemeToggle theme={theme} onToggle={toggle} toDarkLabel={V2_COPY.toDark} toLightLabel={V2_COPY.toLight} />
      {me === null ? null : (
        <Menu
          look="avatar"
          label={V2_COPY.accountMenu}
          trigger={<Avatar name={me.name} />}
          items={[{ key: "sign-out", label: V2_COPY.signOut, onSelect: onSignOut }]}
        />
      )}
    </AppHeader>
  );
}
