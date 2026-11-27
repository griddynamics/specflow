"use client";
// Box: connect with the caller's own Box account (per-user OAuth, docs/box/oauth.md) and bind it to the project. If the caller is connected already,
// the button only binds. Graphico only reads Box, though Box's consent page says "Read and write": the dialog says so before the sign-in. Everything
// the app is used for (a Box link) is added on the canvas.
import { useState } from "react";
import { Button, Notice, Row } from "@graphico/ui";
import { usePorts } from "@/lib/data/provider";
import { HowTo } from "../HowTo";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";
import { CANVAS_COPY } from "../canvas/canvas-copy";
import { V2_COPY } from "../v2-copy";

export function BoxSetup(props: Readonly<SetupProps>) {
  const { box } = usePorts();
  const [busy, setBusy] = useState(false);
  const { integration: i } = props;
  const reconnect = (i.connectedBy !== null || i.workspace !== null) && i.health !== "active";
  const label = reconnect ? V2_COPY.reconnectBox : V2_COPY.connectBox;

  const connect = async () => {
    setBusy(true);
    try {
      const mine = await box.connection();
      if (mine.status === "active") {
        await props.onConnect();
        setBusy(false);
      } else {
        window.location.assign(await box.authorize(props.returnTo)); // the page is left; it returns with ?box=<code>
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
          <HowTo steps={CANVAS_COPY.howBox} />
          <Notice>{V2_COPY.boxReadsOnly}</Notice>
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
