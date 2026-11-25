"use client";
// Confluence link: the link of a page or a space of the project's Atlassian site and "Follow links", a check that counts what an import would
// take, then Add source (one source per link, D5; read once, D7). The acting account is the project's Atlassian integration, so a project
// without it is sent to Integrations from the plus dialog ("Set up first").
import { useId, useState } from "react";
import { Button, Field, Notice, PickRow, TextInput } from "@graphico/ui";
import type { ConfluenceCheck } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { confluenceCheckText, confluenceFailureText } from "./confluence-logic";
import { ConnectFrame } from "./ConnectFrame";

type Busy = "check" | "add" | null;
interface Passed {
  check: ConfluenceCheck;
  /** The values the check was made with: a different link or choice is a new check. */
  link: string;
  follow: boolean;
}

export function ConfluenceForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [link, setLink] = useState("");
  const [follow, setFollow] = useState(true);
  const [passed, setPassed] = useState<Passed | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const linkId = useId();
  const labels = spec.check ?? { test: "", testing: "", add: "" };
  const checked = passed !== null && passed.link === link.trim() && passed.follow === follow;

  const change = (apply: () => void) => {
    apply();
    setPassed(null);
    setProblem(null);
  };
  const fail = (e: unknown) => {
    setBusy(null);
    setProblem(confluenceFailureText(e));
  };
  const run = () => {
    if (busy !== null || link.trim() === "") return;
    setProblem(null);
    const input = { link: link.trim(), follow };
    if (checked) {
      setBusy("add");
      sources.addConfluence(projectId, input).then(() => {
        reload.current();
        onClose();
      }, fail);
      return;
    }
    setBusy("check");
    sources.checkConfluence(projectId, input).then((result) => {
      setBusy(null);
      setPassed({ check: result, ...input });
    }, fail);
  };
  let label = checked ? labels.add : labels.test;
  if (busy === "check") label = labels.testing;
  if (busy === "add") label = CANVAS_COPY.confluenceAdding;
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        run();
      }}
      action={
        <Button type="submit" disabled={busy !== null || link.trim() === ""} aria-busy={busy !== null || undefined}>
          {label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.confluenceLink} htmlFor={linkId}>
        <TextInput
          id={linkId}
          value={link}
          spellCheck={false}
          onChange={(event) => {
            change(() => {
              setLink(event.target.value);
            });
          }}
        />
      </Field>
      <PickRow
        label={CANVAS_COPY.confluenceFollow}
        checked={follow}
        disabled={busy !== null}
        onChange={(on) => {
          change(() => {
            setFollow(on);
          });
        }}
      />
      <Notice>{CANVAS_COPY.confluenceNote}</Notice>
      <div role="status">{checked ? <Notice>{confluenceCheckText(passed.check, follow)}</Notice> : null}</div>
      {problem === null ? null : <Notice role="alert">{problem}</Notice>}
    </ConnectFrame>
  );
}
