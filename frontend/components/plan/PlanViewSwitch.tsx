"use client";
// FE-PLAN-05, MCP-M10 U5: the Plan | Work | Develop switch, shared by the idle screen (BeforePlanView) and the ready screen (PlanReadyView). The tab comes from
// the URL (`?view=plan|develop`; missing or unknown shows Plan); picking a segment pushes the URL with the native history API, which Next.js syncs with
// `useSearchParams`, so `/projects/{id}/plan` with the Develop selection works in both stages.
import { useSearchParams } from "next/navigation";
import { useMemo } from "react";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { parsePlanQuery, type PlanView } from "@/lib/routes";
import { usePlanRoutes } from "./use-plan-ports";

const VIEW_OPTIONS: { value: PlanView; label: string }[] = [
  { value: "plan", label: "Plan" },
  { value: "work", label: "Work" },
  { value: "develop", label: "Develop" },
];

/** The selected view of the plan page, read from the URL. */
export function usePlanView(): PlanView {
  const query = useSearchParams();
  return useMemo(() => parsePlanQuery(new URLSearchParams(query.toString())).view ?? "plan", [query]);
}

export function PlanViewSwitch({ projectId, view }: Readonly<{ projectId: string; view: PlanView }>) {
  const routes = usePlanRoutes();
  return (
    <SegmentedControl
      label="Plan view"
      options={VIEW_OPTIONS}
      value={view}
      onChange={(v) => {
        if (v !== view) window.history.pushState(null, "", routes.plan(projectId, { view: v as PlanView }));
      }}
    />
  );
}
