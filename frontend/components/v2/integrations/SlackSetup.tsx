"use client";
// Slack: the app is added to the workspace and the channel is bound from inside Slack with `/specflow connect <project id>`, so the dialog
// shows the command and waits for the channel. Test connection posts the welcome message in it (docs/mission-control/integrations-api.md).
import { useEffect, useState } from "react";
import { Button, CodeLine, Notice, Row } from "@graphico/ui";
import { usePorts } from "@/lib/data/provider";
import { slackConnectCommand, type SlackChannel } from "@/lib/domain/slack";
import { HowTo } from "../HowTo";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";
import { channelLabel, slackTestFailure, SLACK_POLL_MS } from "./slack-logic";
import { CANVAS_COPY } from "../canvas/canvas-copy";
import { V2_COPY } from "../v2-copy";

export function SlackSetup(props: Readonly<SetupProps>) {
  const { slack } = usePorts();
  const { integration: i, projectId, onRefresh } = props;
  const bound = i.workspace !== null || i.connectedBy !== null;
  const [channel, setChannel] = useState<SlackChannel | null>(null);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  // Until the channel is bound the dialog asks again every few seconds: the person is in Slack meanwhile.
  useEffect(() => {
    if (!i.available || bound) return;
    const timer = setInterval(onRefresh, SLACK_POLL_MS);
    return () => {
      clearInterval(timer);
    };
  }, [i.available, bound, onRefresh]);

  useEffect(() => {
    if (!bound) return;
    let live = true;
    slack.get(projectId).then(
      (s) => {
        if (live) setChannel(s.channel);
      },
      () => undefined, // the channel name is only a caption: the dialog works without it
    );
    return () => {
      live = false;
    };
  }, [slack, projectId, bound]);

  const test = () => {
    setTesting(true);
    setResult(null);
    slack.test(projectId).then(
      (r) => {
        setTesting(false);
        setResult({ ok: true, text: `${V2_COPY.slackTested} ${channelLabel(r.channel)}.` });
      },
      (e: unknown) => {
        setTesting(false);
        setResult({ ok: false, text: slackTestFailure(e) });
      },
    );
  };

  if (!i.available) {
    return (
      <SetupFrame props={props}>
        <Notice>{V2_COPY.slackNotOnServer}</Notice>
      </SetupFrame>
    );
  }
  return (
    <SetupFrame props={props}>
      {bound ? (
        <>
          {channel === null ? null : <Notice>{channelLabel(channel)}</Notice>}
          <Row>
            <Button onClick={test} disabled={testing}>
              {V2_COPY.slackTest}
            </Button>
          </Row>
          {result === null ? null : <Notice role={result.ok ? "status" : "alert"}>{result.text}</Notice>}
        </>
      ) : (
        <>
          <Notice>{V2_COPY.slackSteps}</Notice>
          <CodeLine>{slackConnectCommand(projectId)}</CodeLine>
          <HowTo steps={CANVAS_COPY.howSlackConnect} />
          <Notice>{V2_COPY.slackWaiting}</Notice>
        </>
      )}
    </SetupFrame>
  );
}
