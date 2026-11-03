"use client";
// MCP-M10 (U1, U2): the header of every Guides page is the breadcrumb, placed in the shell's header row (ShellHeader, the pattern Plan,
// Decisions and Connections use), so it sits on the project picker's axis. The hub shows "Guides" (the current page); a topic shows
// "Guides › topic", where "Guides" links to the hub.
import { ShellHeader } from "@/components/shell/ShellHeader";
import { Breadcrumbs } from "@/components/ui/Breadcrumbs";
import { GUIDES_CRUMB } from "@/lib/guides/content";
import { routesFor } from "@/lib/routes";

/** `title` is the topic's title; without it the crumb is the hub itself. */
export function GuideCrumbs({ title }: Readonly<{ title?: string }>) {
  const hub = { label: GUIDES_CRUMB, href: routesFor("production").guides() };
  return (
    <ShellHeader
      tabs={<Breadcrumbs label="Guides path" items={title === undefined ? [{ label: hub.label }] : [hub, { label: title }]} />}
    />
  );
}
