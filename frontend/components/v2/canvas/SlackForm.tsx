"use client";
// Slack channel: the app is invited to the channel and the channel is connected from inside Slack with the command of main
// (`/specflow connect <project id>`), so the form shows the two commands and waits for the channel; it then names it. One channel per project.
// Step 3 posts the welcome message in the bound channel (`slack.test`), says where it went, and then the channel is added.
import { useEffect, useState } from "react";
import { Button, CopyRow, Notice, StepList } from "@graphico/ui";
import { SETTINGS_COPY } from "@/components/settings/settings-copy";
import { slackConnectCommand, type SlackChannel } from "@/lib/domain/slack";
import { channelLabel, slackTestFailure, SLACK_POLL_MS } from "../integrations/slack-logic";
import type { CheckLabels, FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

const COPY_ROW = { copyLabel: CANVAS_COPY.driveCopy, copiedLabel: CANVAS_COPY.driveCopied } as const;
/** After this many reads in a row that fail, the form says so instead of waiting on. */
const MAX_MISSES = 3;
const INVITE_COMMAND = "/invite @SpecFlow";

export interface SlackStepsProps {
  projectId: string;
  /** The bound channel, once the command has run in Slack. */
  channel: SlackChannel | null;
  unreachable: boolean;
  labels: CheckLabels;
  testing: boolean;
  /** What the last attempt to post said: where it went, or why not. */
  result: { ok: boolean; text: string } | null;
  onTest: () => void;
  onAdd: () => void;
  onBack: () => void;
}

/** The three steps of the form: invite, connect (waits for the binding), and the welcome message; "Add channel" follows a message that was posted. */
export function SlackSteps(p: Readonly<SlackStepsProps>) {
  const posted = p.result?.ok === true;
  const action =
    posted || p.channel === null ? (
      <Button disabled={!posted} onClick={p.onAdd}>
        {posted ? p.labels.add : p.labels.test}
      </Button>
    ) : (
      <Button disabled={p.testing} aria-busy={p.testing || undefined} onClick={p.onTest}>
        {p.testing ? p.labels.testing : p.labels.test}
      </Button>
    );
  return (
    <ConnectFrame onBack={p.onBack} action={action}>
      <StepList>
        <li>{CANVAS_COPY.slackInvite}</li>
      </StepList>
      <CopyRow {...COPY_ROW} text={INVITE_COMMAND} name={CANVAS_COPY.slackInviteName} />
      <StepList start={2}>
        <li>{CANVAS_COPY.slackConnect}</li>
      </StepList>
      <CopyRow {...COPY_ROW} text={slackConnectCommand(p.projectId)} name={CANVAS_COPY.slackConnectName} />
      <Notice>{SETTINGS_COPY.slackConnectHint}</Notice>
      {p.channel === null ? (
        <Notice role={p.unreachable ? "alert" : "status"}>{p.unreachable ? V2_COPY.slackUnreachable : V2_COPY.slackWaiting}</Notice>
      ) : (
        <Notice>{`${channelLabel(p.channel)} ${CANVAS_COPY.slackConnected}`}</Notice>
      )}
      <StepList start={3}>
        <li>{CANVAS_COPY.slackTestStep}</li>
      </StepList>
      {p.result === null ? null : <Notice role={p.result.ok ? "status" : "alert"}>{p.result.text}</Notice>}
    </ConnectFrame>
  );
}

export function SlackForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { slack, projects } = useV2Ports();
  const [available, setAvailable] = useState(true);
  const [channel, setChannel] = useState<SlackChannel | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [name, setName] = useState<string | null>(null);
  const labels = spec.check ?? { test: "", testing: "", add: "" };

  // The project's name, for the line that says what was posted; the form works without it.
  useEffect(() => {
    let alive = true;
    projects.get(projectId).then(
      (p) => {
        if (alive) setName(p.name);
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [projects, projectId]);

  // The person is in Slack meanwhile: ask every few seconds until the channel is bound; then Test connection is on.
  useEffect(() => {
    let alive = true;
    let misses = 0;
    let bound = false;
    const ask = () => {
      if (bound) return; // nothing more to wait for
      slack.get(projectId).then(
        (s) => {
          if (!alive) return;
          misses = 0;
          setUnreachable(false);
          setAvailable(s.available);
          if (s.channel !== null) {
            setChannel(s.channel);
            reload.current();
            bound = true;
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

  const test = () => {
    if (testing) return; // a second press while the message is going out
    setTesting(true);
    setResult(null);
    slack.test(projectId).then(
      (r) => {
        setTesting(false);
        const template = name === null ? CANVAS_COPY.slackTestPostedNoName : CANVAS_COPY.slackTestPosted;
        setResult({ ok: true, text: template.replace("{channel}", channelLabel(r.channel)).replace("{name}", name ?? "") });
      },
      (e: unknown) => {
        setTesting(false);
        setResult({ ok: false, text: slackTestFailure(e) });
      },
    );
  };

  if (!available) {
    return (
      <ConnectFrame onBack={onBack}>
        <Notice>{V2_COPY.slackNotOnServer}</Notice>
      </ConnectFrame>
    );
  }
  return (
    <SlackSteps
      projectId={projectId}
      channel={channel}
      unreachable={unreachable}
      labels={labels}
      testing={testing}
      result={result}
      onTest={test}
      onAdd={onClose}
      onBack={onBack}
    />
  );
}
