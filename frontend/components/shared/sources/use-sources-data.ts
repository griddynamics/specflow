"use client";
// What the sources screen reads from the ports and routes: the status (read again after every change), the Drive account email and the
// member's own id. The screen component itself makes no data-access call.
import { useEffect, useRef, useState } from "react";
import { usePorts, useRoutes } from "@/lib/data/provider";
import type { KnowledgePort, ProjectSourcesStatus, SourcesPort } from "@/lib/domain/sources";

/** The app routes. */
export function useSourcesRoutes() {
  return useRoutes();
}

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

/** The Drive account email, for the Drive reasons in the details header (FR-SRC-0008.AC2); null while unknown or unreadable. */
export function useDriveEmail(sources: SourcesPort): string | null {
  const [driveEmail, setDriveEmail] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    sources.driveIdentity().then(
      (i) => {
        if (alive) setDriveEmail(i.ok ? i.email : null);
      },
      () => {
        /* the email stays unknown */
      },
    );
    return () => {
      alive = false;
    };
  }, [sources]);
  return driveEmail;
}

/** Who I am: only the member who added a GitHub repository gives it a token (D4). "" until known. */
export function useMemberId(): string {
  const { people } = usePorts();
  const [me, setMe] = useState("");
  useEffect(() => {
    let alive = true;
    people.me().then(
      (user) => {
        if (alive) setMe(user.id);
      },
      () => {
        /* the id stays unknown */
      },
    );
    return () => {
      alive = false;
    };
  }, [people]);
  return me;
}
