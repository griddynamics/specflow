"use client";
// FE-MC-01..05, FE-DEMO-05, FE-DS-08 (PLAN §a.5, TECH-SPECS §1.1, §5.2; GDRIVE plan 07, owner K5): the Mission Control screen is the
// sources screen in both trees. The old upload-only screen (Empty -> Files selected -> Processing -> Ready, driven by browser parse
// and summarize calls) is gone: uploads are parsed on the server now and appear as the Local files card. The path, the export name,
// the `projectId` prop and the root marker `screen-mission-control` are bound by the page file.
import { usePorts } from "@/lib/data/provider";
import { SourcesMissionControl } from "./sources/SourcesMissionControl";
import "./mc-gaps.css";

/** `key={projectId}`: another project starts with a clean screen state. */
export function MissionControlScreen({ projectId }: Readonly<{ projectId: string }>) {
  const { sources, knowledge } = usePorts();
  return <SourcesMissionControl key={projectId} projectId={projectId} sources={sources} knowledge={knowledge} />;
}
