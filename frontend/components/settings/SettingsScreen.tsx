"use client";
// FR-PRJ-UI-02 (docs/projects/ui.md#header): the Settings screen. The tab bar ("General", "Jira", "Slack", "GitHub") goes into the shell header row
// through <ShellHeader tabs>, level with the project picker; the body has no H1, no title and no tab row. `?tab=` is pushed like the Plan
// switch (native history API, which Next.js syncs with `useSearchParams`); a missing or unknown tab shows General.
import { useSearchParams } from "next/navigation";
import { useMemo } from "react";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { COPY } from "@/lib/copy";
import { parseSettingsQuery, type SettingsTab } from "@/lib/routes";
import { GeneralTab } from "./GeneralTab";
import { GitHubTab } from "./GitHubTab";
import { JiraTab } from "./JiraTab";
import { SlackTab } from "./SlackTab";
import { useSettingsRoutes } from "./use-settings-ports";
import "./settings.css";

const TAB_OPTIONS: { value: SettingsTab; label: string }[] = [
  { value: "general", label: COPY.settingsTabGeneral },
  { value: "jira", label: COPY.settingsTabJira },
  { value: "slack", label: COPY.settingsTabSlack },
  { value: "github", label: COPY.settingsTabGitHub },
];

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
        <TabBody tab={tab} projectId={projectId} projectName={projectName} />
      </div>
    </>
  );
}

function TabBody({ tab, projectId, projectName }: Readonly<{ tab: SettingsTab; projectId: string; projectName: string | null }>) {
  if (tab === "jira") return <JiraTab projectId={projectId} projectName={projectName} />;
  if (tab === "slack") return <SlackTab projectId={projectId} />;
  if (tab === "github") return <GitHubTab projectId={projectId} />;
  return <GeneralTab projectId={projectId} projectName={projectName} />;
}
