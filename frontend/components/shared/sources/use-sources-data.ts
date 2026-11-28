"use client";
// What the sources screen reads from the ports and routes: the status (read again after every change). The screen component
// itself makes no data-access call.
import { useEffect, useRef, useState } from "react";
import type { KnowledgePort, ProjectSourcesStatus } from "@/lib/domain/sources";

/** The status, read again after every change the port reports. A late answer of an older read never overwrites a newer one. */
export function useSourcesStatus(knowledge: KnowledgePort, projectId: string) {
  const [status, setStatus] = useState<ProjectSourcesStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const reload = useRef<() => void>(() => {
    /* nothing to read again before the first read */
  });
  useEffect(() => {
    let alive = true;
    let seq = 0;
    const load = () => {
      const mine = ++seq;
      knowledge.status(projectId).then(
        (s) => {
          if (!alive || mine !== seq) return;
          setStatus(s);
          setFailed(false);
        },
        () => {
          if (alive && mine === seq) setFailed(true);
        },
      );
    };
    const off = knowledge.subscribe(projectId, load);
    reload.current = load;
    load();
    return () => {
      alive = false;
      reload.current = () => {
        /* the screen is gone */
      };
      off();
      knowledge.leave(projectId);
    };
  }, [projectId, knowledge]);
  return { status, failed, reload };
}
