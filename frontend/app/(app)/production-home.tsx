"use client";
// FE-BP-03.AC1, AC2 (TECH-SPECS §1.6, O12, PA17, DECISIONS G01-14): production `/`. The backend lists projects newest first, so the
// "last project" is list[0]: its Mission Control opens (Empty, Processing or Ready). With no project an EmptyState offers "New",
// which opens the shell's Create project modal (the name goes to POST /projects, A9). A failed list shows a generic message; the
// error body never reaches the page (DataError carries a code only).
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useCreateProjectSlot } from "@/components/shell/CreateProjectSlot";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/Icon";
import { Spinner } from "@/components/ui/ProgressBar";
import { usePorts, useRoutes } from "@/lib/data/provider";

export const HOME_EMPTY_TITLE = "No projects yet";
export const HOME_EMPTY_BODY = "Create your first project, then upload its files in Mission Control.";
export const HOME_ERROR = "The projects could not be loaded. Reload the page to try again.";

export function ProductionHome() {
  const { projects } = usePorts();
  const routes = useRoutes();
  const router = useRouter();
  const slot = useCreateProjectSlot();
  const [state, setState] = useState<"loading" | "empty" | "error">("loading");

  useEffect(() => {
    let live = true;
    projects.list().then(
      (list) => {
        if (!live) return;
        if (list.length > 0) router.replace(routes.missionControl(list[0].id));
        else setState("empty");
      },
      () => {
        if (live) setState("error");
      },
    );
    return () => {
      live = false;
    };
  }, [projects, router, routes]);

  if (state === "loading") {
    return (
      <div className="shell__loading" aria-busy="true" data-testid="home-loading">
        <Spinner size={24} label="Opening your last project" />
      </div>
    );
  }
  if (state === "error") {
    return (
      <p className="body shell__note" role="alert" data-testid="home-error">
        {HOME_ERROR}
      </p>
    );
  }
  return (
    <div className="home-scene gdg-scene" data-testid="home-empty">
      <div role="heading" aria-level={1} className="title-1 home-sr">
        Projects
      </div>
      <EmptyState
        icon="folder-simple"
        title={HOME_EMPTY_TITLE}
        actions={
          <Button
            onClick={() => {
              slot.open();
            }}
            data-testid="home-new"
          >
            <Icon name="plus" size={14} />
            New project
          </Button>
        }
      >
        {HOME_EMPTY_BODY}
      </EmptyState>
    </div>
  );
}
