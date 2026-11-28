"use client";
// FE-SHELL-02, FE-PPL-01, FE-DEMO-04 (TECH-SPECS §1.8, §2.3, §2.4): what the shell reads from the ports. Ports only, no fixtures.
import { useEffect, useState } from "react";
import type { Person } from "@/lib/domain/people";
import type { Project } from "@/lib/domain/projects";
import { usePorts } from "@/lib/data/provider";

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

/**
 * The switcher list, newest first. Re-reads when the port says the list or a status changed, and when `refreshKey` changes
 * (Settings: a collaborator left, which the port does not announce). null until loaded.
 */
export function useProjectList(refreshKey?: string): Project[] | null {
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
  }, [projects, refreshKey]);
  return list;
}
