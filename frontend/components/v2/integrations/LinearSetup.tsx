"use client";
// Linear: connect with the caller's own account (per-user OAuth, as the Atlassian card does) and bind it to the project. If the caller is
// connected already, the button only binds. A connection that expired or was revoked (the workspace withdrew the app) says Reconnect. The workspace, the member and Disconnect come from the
// shared frame. Everything Linear is used for (a plan, gap questions, a project as a source) is set up on the canvas.
import { useState } from "react";
import { Button, Row } from "@graphico/ui";
import { usePorts } from "@/lib/data/provider";
import { HowTo } from "../HowTo";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";
import { CANVAS_COPY } from "../canvas/canvas-copy";
import { V2_COPY } from "../v2-copy";

export function LinearSetup(props: Readonly<SetupProps>) {
  const { linear } = usePorts();
  const [busy, setBusy] = useState(false);
  const { integration: i } = props;
  const reconnect = (i.connectedBy !== null || i.workspace !== null) && i.health !== "active";
  const label = reconnect ? V2_COPY.reconnectLinear : V2_COPY.connectLinear;

  const connect = async () => {
    setBusy(true);
    try {
      const mine = await linear.connection();
      if (mine.status === "active") {
        await props.onConnect();
        setBusy(false);
      } else {
        window.location.assign(await linear.authorize(props.returnTo)); // the page is left; it returns with ?linear=<code>
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
          <HowTo steps={CANVAS_COPY.howLinear} />
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
