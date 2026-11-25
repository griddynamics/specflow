"use client";
// Atlassian: connect the site with the caller's own account (per-user OAuth, as in the Jira tab) and bind it to the project. If the
// caller is connected already, the button only binds. Everything the app is used for (a Confluence link, a Jira epic) is added on the canvas.
import { useState } from "react";
import { Button, Row } from "@graphico/ui";
import { usePorts } from "@/lib/data/provider";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";
import { V2_COPY } from "../v2-copy";

export function AtlassianSetup(props: Readonly<SetupProps>) {
  const { jira } = usePorts();
  const [busy, setBusy] = useState(false);
  const { integration: i } = props;
  const reconnect = (i.connectedBy !== null || i.workspace !== null) && i.health !== "active";
  const label = reconnect ? V2_COPY.reconnectAtlassian : V2_COPY.connectAtlassian;

  const connect = async () => {
    setBusy(true);
    try {
      const mine = await jira.connection();
      if (mine.status === "active") {
        await props.onConnect();
        setBusy(false);
      } else {
        window.location.assign(await jira.authorize(props.returnTo)); // the page is left; it returns with ?atlassian=<code>
      }
    } catch (e: unknown) {
      setBusy(false);
      props.onError(e);
    }
  };

  return (
    <SetupFrame props={props}>
      {i.health === "active" ? null : (
        <Row>
          <Button onClick={() => void connect()} disabled={busy}>
            {busy ? V2_COPY.connecting : label}
          </Button>
        </Row>
      )}
    </SetupFrame>
  );
}
