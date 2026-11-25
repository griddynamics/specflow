"use client";
// The part every integration's set-up dialog shares: what the app is connected as, the rule it runs by, the Disconnect confirmation and the
// foot with Done. The body of the app (connect button, repositories, the Slack command) is the children.
import { useState, type ReactNode } from "react";
import { Button, ConfirmPanel, DialogFoot, Notice } from "@graphico/ui";
import type { SetupProps } from "./setup-props";
import { V2_COPY } from "../v2-copy";

export function SetupFrame({ props, children }: Readonly<{ props: SetupProps; children: ReactNode }>) {
  const { integration: i } = props;
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const bound = i.connectedBy !== null || i.workspace !== null;
  const stopped = bound && i.health !== "active";
  const line = [i.workspace?.name, i.connectedBy === null ? undefined : `${V2_COPY.connectedBy} ${i.connectedBy.name}`]
    .filter(Boolean)
    .join(" · ");
  return (
    <>
      {bound && line !== "" ? <Notice>{line}</Notice> : null}
      {stopped ? <Notice>{V2_COPY.stoppedBody}</Notice> : null}
      {children}
      <Notice>{V2_COPY.actsAs}</Notice>
      {asking ? (
        <ConfirmPanel
          title={`${V2_COPY.disconnect} ${V2_COPY.appNames[i.app]}?`}
          body={V2_COPY.disconnectBody}
          confirmLabel={V2_COPY.disconnect}
          keepLabel={V2_COPY.keep}
          busy={busy}
          onKeep={() => {
            setAsking(false);
          }}
          onConfirm={() => {
            setBusy(true);
            void props.onDisconnect().then((done) => {
              setBusy(false);
              if (done) props.onClose();
              setAsking(false);
            });
          }}
        />
      ) : null}
      <DialogFoot>
        {props.canDisconnect && !asking ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setAsking(true);
            }}
          >
            {V2_COPY.disconnect}
          </Button>
        ) : (
          <span />
        )}
        <Button variant="ghost" onClick={props.onDone}>
          {V2_COPY.done}
        </Button>
      </DialogFoot>
    </>
  );
}
