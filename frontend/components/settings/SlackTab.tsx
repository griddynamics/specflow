"use client";
// docs/slack/gap-answering.md "The channel flow": the Slack tab. It shows only this project's channel. The owner connects one from inside
// Slack with the command shown here (`/specflow connect <project id>`), so only someone in the channel can connect it, and a channel
// belongs to one project; the owner can disconnect it here. A collaborator sees the channel read-only. Without the Slack app on the
// server the tab says so.
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { Icon } from "@/components/shared/ui/Icon";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/shared/ui/Toast";
import { isDataError } from "@/lib/domain/errors";
import { type SlackSettings, slackConnectCommand } from "@/lib/domain/slack";
import { LoadFailed } from "./parts";
import { SETTINGS_COPY } from "@/components/shared/settings/settings-copy";
import { useRemoveRight } from "./use-remove-right";
import { useSlackPort } from "@/components/shared/settings/use-settings-ports";

const channelLabel = (name: string, isPrivate: boolean) => `${isPrivate ? "🔒" : "#"} ${name}`;

export function SlackTab({ projectId }: Readonly<{ projectId: string }>) {
  const slack = useSlackPort();
  const mayRemove = useRemoveRight(projectId, "slack");
  const toast = useToast();
  const [settings, setSettings] = useState<SlackSettings | "failed" | null>(null);
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const load = useCallback(() => {
    slack.get(projectId).then(
      (s) => {
        if (live.current) setSettings(s);
      },
      () => {
        if (live.current) setSettings("failed");
      },
    );
  }, [slack, projectId]);
  useEffect(() => {
    load();
  }, [load]);

  if (settings === null) {
    return (
      <div aria-hidden data-testid="settings-loading">
        <Skeleton variant="block" height={160} />
      </div>
    );
  }
  if (settings === "failed") {
    return (
      <LoadFailed
        text={SETTINGS_COPY.slackLoadFailed}
        onRetry={() => {
          setSettings(null);
          load();
        }}
      />
    );
  }
  if (!settings.available) {
    return (
      <p className="settings-note" role="note" data-testid="slack-unavailable">
        <Icon name="plug" size={20} />
        {SETTINGS_COPY.slackUnavailable}
      </p>
    );
  }

  const unbind = () => {
    setBusy(true);
    slack.unbind(projectId).then(
      () => {
        if (!live.current) return;
        setBusy(false);
        setSettings({ ...settings, channel: null });
        toast({ title: SETTINGS_COPY.slackUnbound, tone: "success" });
      },
      (e: unknown) => {
        if (!live.current) return;
        setBusy(false);
        const forbidden = isDataError(e) && e.detail?.apiCode === "integration_forbidden";
        toast({ title: forbidden ? SETTINGS_COPY.slackRemoveForbidden : SETTINGS_COPY.slackSaveFailed, tone: "danger" });
      },
    );
  };

  const bound = settings.channel;
  if (bound !== null) {
    return (
      <div className="settings-sec" data-testid="slack-tab">
        <p data-testid="slack-bound">{channelLabel(bound.name, bound.private)}</p>
        {mayRemove ? (
          <div>
            <Button variant="glass" disabled={busy} onClick={unbind} data-testid="slack-unbind">
              {SETTINGS_COPY.slackUnbind}
            </Button>
          </div>
        ) : null}
        <span className="settings-hint">{SETTINGS_COPY.slackHint}</span>
      </div>
    );
  }
  if (!settings.editable) {
    return (
      <div className="settings-sec" data-testid="slack-tab">
        <p data-testid="slack-bound">{SETTINGS_COPY.slackNone}</p>
      </div>
    );
  }
  return (
    <div className="settings-sec" data-testid="slack-tab">
      <p className="settings-hint">{SETTINGS_COPY.slackConnectIntro}</p>
      <CodeBlock caption={SETTINGS_COPY.slackConnectCaption} text={slackConnectCommand(projectId)} />
      <span className="settings-hint">{SETTINGS_COPY.slackConnectHint}</span>
    </div>
  );
}
