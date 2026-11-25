"use client";
// Slack channel: the app is invited to the channel and the channel is connected from inside Slack with the command of main
// (`/specflow connect <project id>`), so the form shows the two commands and waits for the channel; it then names it. One channel per project.
// The welcome message of a channel (Test connection) is a later connector.
import { useEffect, useState } from "react";
import { Button, CopyRow, Notice, StepList } from "@graphico/ui";
import { SETTINGS_COPY } from "@/components/settings/settings-copy";
import { slackConnectCommand, type SlackChannel } from "@/lib/domain/slack";
import { channelLabel, SLACK_POLL_MS } from "../integrations/slack-logic";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

const COPY_ROW = { copyLabel: CANVAS_COPY.driveCopy, copiedLabel: CANVAS_COPY.driveCopied } as const;
/** After this many reads in a row that fail, the form says so instead of waiting on. */
const MAX_MISSES = 3;
const INVITE_COMMAND = "/invite @SpecFlow";

export function SlackForm({ projectId, reload, onBack, onClose }: Readonly<FormProps>) {
  const { slack } = useV2Ports();
  const [available, setAvailable] = useState(true);
  const [channel, setChannel] = useState<SlackChannel | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  // The person is in Slack meanwhile: ask every few seconds until the channel is bound.
  useEffect(() => {
    let alive = true;
    let misses = 0;
    const ask = () => {
      slack.get(projectId).then(
        (s) => {
          if (!alive) return;
          misses = 0;
          setUnreachable(false);
          setAvailable(s.available);
          if (s.channel !== null) {
            setChannel(s.channel);
            reload.current();
          }
        },
        () => {
          misses += 1;
          if (alive && misses >= MAX_MISSES) setUnreachable(true);
        },
      );
    };
    ask();
    const timer = setInterval(ask, SLACK_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [slack, projectId, reload]);

  if (!available) {
    return (
      <ConnectFrame onBack={onBack}>
        <Notice>{V2_COPY.slackNotOnServer}</Notice>
      </ConnectFrame>
    );
  }
  if (channel !== null) {
    return (
      <ConnectFrame
        action={
          <Button variant="ghost" onClick={onClose}>
            {CANVAS_COPY.done}
          </Button>
        }
      >
        <Notice>{`${channelLabel(channel)} ${CANVAS_COPY.slackConnected}`}</Notice>
      </ConnectFrame>
    );
  }
  return (
    <ConnectFrame onBack={onBack}>
      <StepList>
        <li>{CANVAS_COPY.slackInvite}</li>
      </StepList>
      <CopyRow {...COPY_ROW} text={INVITE_COMMAND} name={CANVAS_COPY.slackInviteName} />
      <StepList start={2}>
        <li>{CANVAS_COPY.slackConnect}</li>
      </StepList>
      <CopyRow {...COPY_ROW} text={slackConnectCommand(projectId)} name={CANVAS_COPY.slackConnectName} />
      <Notice>{SETTINGS_COPY.slackConnectHint}</Notice>
      <Notice role={unreachable ? "alert" : "status"}>{unreachable ? V2_COPY.slackUnreachable : V2_COPY.slackWaiting}</Notice>
    </ConnectFrame>
  );
}
