// The frame of a connection form inside the plus dialog: the form's body, then the foot with Back on the left and the form's action on the right.
import type { ReactNode, SubmitEvent } from "react";
import { Button, DialogFoot, Stack } from "@graphico/ui";
import { CANVAS_COPY } from "./canvas-copy";

export function ConnectFrame({
  onBack,
  action,
  onSubmit,
  children,
}: Readonly<{ onBack?: () => void; action?: ReactNode; onSubmit?: (event: SubmitEvent) => void; children: ReactNode }>) {
  return (
    <Stack onSubmit={onSubmit}>
      {children}
      <DialogFoot>
        {onBack === undefined ? (
          <span />
        ) : (
          <Button variant="ghost" size="sm" onClick={onBack}>
            {CANVAS_COPY.back}
          </Button>
        )}
        {action ?? <span />}
      </DialogFoot>
    </Stack>
  );
}
