"use client";
// FE-SHELL-02, FE-PPL-01, FE-DEMO-04 (TECH-SPECS §1.8, §2.3, §2.4): what the shell reads from the ports. Ports only, no fixtures.
import { useEffect, useEffectEvent, useState } from "react";
import type { Person } from "@/lib/domain/people";
import type { Project } from "@/lib/domain/projects";
import { useCurrentProject, usePorts, useRoutes, useSetCurrentProject } from "@/lib/data/provider";
import { gateOutcome } from "./shell-logic";

/** The active user (GET /me). null until the port answers. */
export function useMe(): Person | null {
  const { people } = usePorts();
  const [me, setMe] = useState<Person | null>(null);
  useEffect(() => {
    let live = true;
    people.me().then(
      (p) => {
        if (live) setMe(p);
      },
      () => undefined, // the top bar stays a placeholder; the page itself is not blocked
    );
    return () => {
      live = false;
    };
  }, [people]);
  return me;
}

/** The switcher list, newest first. Re-reads when the port says the list or a status changed. null until loaded. */
export function useProjectList(): Project[] | null {
  const { projects } = usePorts();
  const [list, setList] = useState<Project[] | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => {
      projects.list().then(
        (l) => {
          if (live) setList(l);
        },
        () => undefined,
      );
    };
    load();
    const off = projects.subscribe(load);
    return () => {
      live = false;
      off();
    };
  }, [projects]);
  return list;
}

export type Gate = { id: string; status: "ok" | "error" } | null;

/**
 * §1.8, PA27: asks `projects.get(projectId)` when a project-scoped route mounts. `onRedirect` runs for `not_found` or
 * `validation`; any other failure becomes `status: "error"`. The result belongs to the id it was asked for, so a route change
 * never shows the previous project's answer.
 */
export function useProjectGate(projectId: string | undefined, onRedirect: () => void): Gate {
  const { projects } = usePorts();
  const [gate, setGate] = useState<Gate>(null);
  const redirect = useEffectEvent(onRedirect);
  useEffect(() => {
    if (projectId === undefined) return;
    let live = true;
    projects.get(projectId).then(
      () => {
        if (live) setGate({ id: projectId, status: "ok" });
      },
      (e: unknown) => {
        if (!live) return;
        if (gateOutcome(e) === "redirect") redirect();
        else setGate({ id: projectId, status: "error" });
      },
    );
    return () => {
      live = false;
    };
  }, [projects, projectId]);
  return gate;
}

/** The routes and the current project (with its setter), as the shell frame reads them. */
export function useShellEnv() {
  return {
    routes: useRoutes(),
    current: useCurrentProject(),
    setCurrent: useSetCurrentProject(),
  };
}
