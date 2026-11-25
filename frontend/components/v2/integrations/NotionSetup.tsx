"use client";
// Notion: connect with the caller's own account (per-user OAuth) and bind it to the project. Notion's consent page has the page picker, so
// pages are shared there; "Add pages in Notion" goes through it again. If the caller is connected already, the button only binds. Everything
// the app is used for (a page or database link, the plan's database) is added on the canvas.
import { useCallback, useEffect, useState } from "react";
import { Button, Notice, Row } from "@graphico/ui";
import { useToast } from "@/components/ui/Toast";
import { usePorts } from "@/lib/data/provider";
import type { NotionConnection } from "@/lib/domain/notion-connection";
import { HowTo } from "../HowTo";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";
import { NOTION_WORDS, notionSetupViewOf } from "./notion-setup-logic";
import { CANVAS_COPY } from "../canvas/canvas-copy";
import { V2_COPY } from "../v2-copy";

export function NotionSetup(props: Readonly<SetupProps>) {
  const { notionConnection } = usePorts();
  const toast = useToast();
  const [mine, setMine] = useState<NotionConnection | "failed" | null>(null);
  const [busy, setBusy] = useState(false);
  const view = notionSetupViewOf(props.integration, props.canDisconnect, mine === "failed" ? null : mine);

  const load = useCallback(() => {
    notionConnection.connection().then(setMine, () => {
      setMine("failed");
    });
  }, [notionConnection]);
  useEffect(() => {
    load();
  }, [load]);

  const fail = (e: unknown) => {
    setBusy(false);
    props.onError(e);
  };
  const authorize = () => {
    setBusy(true);
    notionConnection.authorize(props.returnTo).then(({ url }) => {
      window.location.assign(url); // the page is left; it returns with ?notion=<code>
    }, fail);
  };
  const connect = () => {
    if (!view.bindsOnly) {
      authorize();
      return;
    }
    setBusy(true);
    void props.onConnect().then(() => {
      setBusy(false);
    });
  };
  const forget = () => {
    setBusy(true);
    notionConnection.disconnect().then(() => {
      setBusy(false);
      toast({ title: NOTION_WORDS.forgotten, tone: "success" });
      load();
      props.onRefresh();
    }, fail);
  };

  return (
    <SetupFrame props={props}>
      {mine === "failed" ? <Notice role="alert">{NOTION_WORDS.loadFailed}</Notice> : null}
      {view.state === "unavailable" ? <Notice>{NOTION_WORDS.unavailable}</Notice> : null}
      {view.howTo ? <HowTo steps={CANVAS_COPY.howNotion} /> : null}
      {view.primary === null ? null : (
        <Row>
          <Button onClick={connect} disabled={busy || view.state === "loading"}>
            {busy ? V2_COPY.connecting : view.primary === "reconnect" ? V2_COPY.reconnectNotion : V2_COPY.connectNotion}
          </Button>
        </Row>
      )}
      {view.addPages || view.forget ? (
        <Row>
          {view.addPages ? (
            <Button variant="ghost" size="sm" onClick={authorize} disabled={busy}>
              {NOTION_WORDS.addPages}
            </Button>
          ) : null}
          {view.forget ? (
            <Button variant="ghost" size="sm" onClick={forget} disabled={busy}>
              {NOTION_WORDS.forget}
            </Button>
          ) : null}
        </Row>
      ) : null}
    </SetupFrame>
  );
}
