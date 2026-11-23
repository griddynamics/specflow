"use client";
// docs/slack/gap-answering.md "The channel flow": the Slack tab. The owner picks a channel the SpecFlow Slack app is in; the project's
// open questions are posted there, and anyone in the channel answers one by replying in its thread. A collaborator sees the bound
// channel read-only. Without the Slack app on the server the tab says so.
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { isDataError } from "@/lib/domain/errors";
import type { SlackSettings } from "@/lib/domain/slack";
import { LoadFailed } from "./parts";
import { SETTINGS_COPY } from "./settings-copy";
import { useSlackPort } from "./use-settings-ports";

const channelLabel = (name: string, isPrivate: boolean) => `${isPrivate ? "🔒" : "#"} ${name}`;

export function SlackTab({ projectId }: Readonly<{ projectId: string }>) {
  const slack = useSlackPort();
  const toast = useToast();
  const [settings, setSettings] = useState<SlackSettings | "failed" | null>(null);
  const [choice, setChoice] = useState<string | undefined>(undefined);
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
        if (!live.current) return;
        setSettings(s);
        setChoice(s.channel?.id);
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

  const failed = (e: unknown) => {
    const code = isDataError(e) ? e.detail?.apiCode : undefined;
    toast({ title: code === "slack_channel_invalid" ? SETTINGS_COPY.slackChannelInvalid : SETTINGS_COPY.slackSaveFailed, tone: "danger" });
  };
  const bind = () => {
    if (choice === undefined) return;
    setBusy(true);
    slack.bind(projectId, choice).then(
      (s) => {
        if (!live.current) return;
        setSettings(s);
        setBusy(false);
        toast({ title: SETTINGS_COPY.slackBound, tone: "success" });
      },
      (e: unknown) => {
        if (!live.current) return;
        setBusy(false);
        failed(e);
      },
    );
  };
  const unbind = () => {
    setBusy(true);
    slack.unbind(projectId).then(
      () => {
        if (!live.current) return;
        setBusy(false);
        setChoice(undefined);
        setSettings({ ...settings, channel: null });
      },
      (e: unknown) => {
        if (!live.current) return;
        setBusy(false);
        failed(e);
      },
    );
  };

  const bound = settings.channel;
  return (
    <div className="settings-sec" data-testid="slack-tab">
      {settings.editable ? (
        <>
          <Select
            label={SETTINGS_COPY.slackChannel}
            placeholder={SETTINGS_COPY.slackChannelPlaceholder}
            options={settings.channels.map((c) => ({ value: c.id, label: channelLabel(c.name, c.private) }))}
            value={choice}
            onChange={setChoice}
          />
          <Button variant="primary" disabled={busy || choice === undefined || choice === bound?.id} onClick={bind} data-testid="slack-bind">
            {SETTINGS_COPY.slackBind}
          </Button>
          {bound === null ? null : (
            <Button variant="glass" disabled={busy} onClick={unbind} data-testid="slack-unbind">
              {SETTINGS_COPY.slackUnbind}
            </Button>
          )}
        </>
      ) : (
        <p data-testid="slack-bound">{bound === null ? SETTINGS_COPY.slackNone : channelLabel(bound.name, bound.private)}</p>
      )}
      <span className="settings-hint">{SETTINGS_COPY.slackHint}</span>
    </div>
  );
}
