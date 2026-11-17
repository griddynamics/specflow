"use client";
// FR-PRJ-UI-02 (docs/projects/ui.md#header): the Settings screen. The tab bar ("General", the only tab) goes into the shell header row
// through <ShellHeader tabs>, level with the project picker; the body has no H1, no title and no tab row. `?tab=` is pushed like the Plan
// switch (native history API, which Next.js syncs with `useSearchParams`); a missing or unknown tab shows General.
import { useSearchParams } from "next/navigation";
import { useMemo } from "react";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { COPY } from "@/lib/copy";
import { parseSettingsQuery, type SettingsTab } from "@/lib/routes";
import { GeneralTab } from "./GeneralTab";
import { useSettingsRoutes } from "./use-settings-ports";
import "./settings.css";

const TAB_OPTIONS: { value: SettingsTab; label: string }[] = [{ value: "general", label: COPY.settingsTabGeneral }];

/** `key={projectId}`: another project starts with a fresh tab state. */
export function SettingsScreen({ projectId, projectName }: Readonly<{ projectId: string; projectName: string | null }>) {
  return <Settings key={projectId} projectId={projectId} projectName={projectName} />;
}

function Settings({ projectId, projectName }: Readonly<{ projectId: string; projectName: string | null }>) {
  const routes = useSettingsRoutes();
  const query = useSearchParams();
  const tab = useMemo(() => parseSettingsQuery(new URLSearchParams(query.toString())).tab, [query]);
  return (
    <>
      <ShellHeader
        tabs={
          <SegmentedControl
            label={COPY.settingsSection}
            options={TAB_OPTIONS}
            value={tab}
            onChange={(v) => {
              if (v !== tab) window.history.pushState(null, "", routes.settings(projectId, { tab: v as SettingsTab }));
            }}
          />
        }
      />
      <div className="settings" data-testid="screen-settings" data-tab={tab}>
        <GeneralTab projectId={projectId} projectName={projectName} /> {/* the only tab; `tab` is read for the segment and the URL */}
      </div>
    </>
  );
}
