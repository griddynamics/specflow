"use client";
// docs/jira-sync/ui.md#plan-line: the Jira sync line of the ready Plan. It shows only while the project's Jira sync is available and on, and
// never gets in the plan's way: a failed read hides it without a toast, and nothing waits for it. The settings are read on mount and every
// POLL_MS while they say the sync is on. `JiraSyncLineView` takes plain props; `JiraSyncLine` wires it to the port and the routes.
import Link from "next/link";
import { useEffect, useState } from "react";
import { JiraProgress } from "@/components/settings/JiraProgress";
import { POLL_MS } from "@/components/settings/jira-logic";
import type { JiraSettings } from "@/lib/domain/jira";
import { usePlanRoutes, useJiraPort } from "../use-plan-ports";
import { JIRA_LINE_COPY } from "./jira-sync-text";
import "./jira-sync-line.css";

/** True when the line has something to say: Jira is connected, the sync is on, and a pass has run or runs. */
export const jiraLineShows = (s: JiraSettings): boolean =>
  s.available && s.enabled && s.jiraProject !== null && s.sync !== null && s.sync.status !== "idle" && s.sync.status !== "skipped";

export function JiraSyncLineView({ settings, settingsHref }: Readonly<{ settings: JiraSettings; settingsHref: string }>) {
  const sync = settings.sync;
  if (sync === null || !jiraLineShows(settings)) return null;
  const epic = settings.epicKey ?? settings.createdEpicKey;
  const settingsLink = (
    <Link className="jira-line__link" href={settingsHref}>
      {JIRA_LINE_COPY.openSettings}
    </Link>
  );
  let body;
  if (sync.status === "running") {
    body =
      sync.progress === null ? <span>{JIRA_LINE_COPY.queued}</span> : <JiraProgress progress={sync.progress} testId="plan-jira-progress" />;
  } else if (sync.status === "succeeded") {
    body = (
      <>
        <span>{JIRA_LINE_COPY.upToDate}</span>
        {settings.siteUrl !== null && epic !== null ? (
          <a
            className="jira-line__link"
            href={`${settings.siteUrl}/browse/${encodeURIComponent(epic)}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            {`${JIRA_LINE_COPY.epic} ${epic}`}
          </a>
        ) : null}
      </>
    );
  } else {
    body = (
      <>
        <span role="status">{sync.status === "partial" ? JIRA_LINE_COPY.partial : JIRA_LINE_COPY.failed}</span>
        {settingsLink}
      </>
    );
  }
  return (
    <div className="jira-line" role="status" aria-label={JIRA_LINE_COPY.label} data-testid="plan-jira" data-status={sync.status}>
      {body}
    </div>
  );
}

/** Reads the Jira settings of the project, again every POLL_MS while the sync is on; a failed read hides the line until a read works. */
function useJiraSettings(projectId: string): JiraSettings | null {
  const jira = useJiraPort();
  const [last, setLast] = useState<JiraSettings | null>(null); // the latest good answer
  const [failed, setFailed] = useState(false);
  const polling = last !== null && last.available && last.enabled;
  useEffect(() => {
    let alive = true;
    const load = () => {
      jira.get(projectId).then(
        (s) => {
          if (!alive) return;
          setLast(s);
          setFailed(false);
        },
        () => {
          if (alive) setFailed(true);
        },
      );
    };
    load();
    const timer = polling ? setInterval(load, POLL_MS) : undefined;
    return () => {
      alive = false;
      if (timer !== undefined) clearInterval(timer);
    };
  }, [jira, projectId, polling]);
  return failed ? null : last;
}

export function JiraSyncLine({ projectId }: Readonly<{ projectId: string }>) {
  const settings = useJiraSettings(projectId);
  const routes = usePlanRoutes();
  return settings === null ? null : <JiraSyncLineView settings={settings} settingsHref={routes.settings(projectId, { tab: "jira" })} />;
}
