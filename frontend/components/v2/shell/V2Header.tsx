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
          <Menu look="project" label={V2_COPY.projectsMenu} trigger={current?.name ?? V2_COPY.projectsMenu} items={items} />
        )
      }
    >
      {canvas === null || integrations === null ? null : (
        <>
          <NavLink href={canvas} current={pathname === canvas} onNavigate={go(canvas)}>
            {V2_COPY.missionControl}
          </NavLink>
          <NavButton opensDialog onClick={onMembers}>
            {V2_COPY.members}
          </NavButton>
          <NavLink href={integrations} current={pathname === integrations} onNavigate={go(integrations)}>
            {V2_COPY.integrations}
          </NavLink>
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
