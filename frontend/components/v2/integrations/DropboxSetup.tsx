"use client";
// Dropbox: connect with the caller's own Dropbox account (per-user OAuth, docs/dropbox/oauth.md) and bind it to the project. If the caller is connected already,
// the button only binds. Dropbox is asked for read scopes only, so the card says Graphico only reads the files. Everything
// the app is used for (a Dropbox link) is added on the canvas.
import { useState } from "react";
import { Button, Notice, Row } from "@graphico/ui";
import { usePorts } from "@/lib/data/provider";
import { HowTo } from "../HowTo";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";
import { CANVAS_COPY } from "../canvas/canvas-copy";
import { V2_COPY } from "../v2-copy";

export function DropboxSetup(props: Readonly<SetupProps>) {
  const { dropbox } = usePorts();
  const [busy, setBusy] = useState(false);
  const { integration: i } = props;
  const reconnect = (i.connectedBy !== null || i.workspace !== null) && i.health !== "active";
  const label = reconnect ? V2_COPY.reconnectDropbox : V2_COPY.connectDropbox;

  const connect = async () => {
    setBusy(true);
    try {
      const mine = await dropbox.connection();
      if (mine.status === "active") {
        await props.onConnect();
        setBusy(false);
      } else {
        window.location.assign(await dropbox.authorize(props.returnTo)); // the page is left; it returns with ?dropbox=<code>
      }
    } catch (e: unknown) {
      setBusy(false);
      props.onError(e);
    }
  };

  return (
    <SetupFrame props={props}>
      {i.health === "active" ? null : (
        <>
          <HowTo steps={CANVAS_COPY.howDropbox} />
          <Notice>{V2_COPY.dropboxReadsOnly}</Notice>
          <Row>
            <Button onClick={() => void connect()} disabled={busy}>
              {busy ? V2_COPY.connecting : label}
            </Button>
          </Row>
        </>
      )}
    </SetupFrame>
  );
}
