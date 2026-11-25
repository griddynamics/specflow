"use client";
// Google Drive: share the folder with the Drive account, paste its link, test it, then add it (the V1 flow, `useDriveFlow`, with its
// reasons and its cutoffs). The folder is read once, when it is added (D7).
import { useId } from "react";
import { Button, CopyRow, Disclosure, Field, Notice, StepList, TextInput } from "@graphico/ui";
import {
  driveReasonMessage,
  emailLabel,
  GUIDE_STEPS,
  GUIDE_TOGGLE,
  NOTHING_TO_IMPORT,
  SAVE_TOO_LONG,
} from "@/components/mission-control/sources/drive-logic";
import { useDriveFlow, type DrivePhase } from "@/components/mission-control/sources/use-drive-flow";
import type { CheckLabels, FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

function primaryLabel(check: CheckLabels, canSave: boolean, phase: DrivePhase): string {
  if (canSave) return phase === "saving" ? CANVAS_COPY.driveSaving : check.add;
  return phase === "testing" ? check.testing : check.test;
}

export function DriveForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const flow = useDriveFlow({
    projectId,
    sources,
    onSaved: () => {
      reload.current();
      onClose();
    },
  });
  const linkId = useId();
  const account = emailLabel(flow.identity);
  const check = spec.check ?? { test: "", testing: "", add: "" };
  const { summary } = flow;
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        if (!flow.blocked) void flow.act();
      }}
      action={
        <Button type="submit" disabled={flow.blocked} aria-busy={flow.busy || undefined}>
          {primaryLabel(check, flow.canSave, flow.phase)}
        </Button>
      }
    >
      <StepList>
        <li>{CANVAS_COPY.driveStepShare}</li>
      </StepList>
      {flow.email === null ? (
        <Notice>{account}</Notice>
      ) : (
        <CopyRow
          text={flow.email}
          copyLabel={CANVAS_COPY.driveCopy}
          copiedLabel={CANVAS_COPY.driveCopied}
          name={CANVAS_COPY.driveAccount}
        />
      )}
      <Disclosure summary={GUIDE_TOGGLE}>
        <StepList>
          {GUIDE_STEPS.map((step) => (
            <li key={step.image}>{step.text.replace("{email}", account)}</li>
          ))}
        </StepList>
      </Disclosure>
      <StepList start={2}>
        <li>{CANVAS_COPY.driveStepPaste}</li>
      </StepList>
      <Field
        label={CANVAS_COPY.driveLink}
        htmlFor={linkId}
        error={flow.badLink ? driveReasonMessage("invalid_link", flow.email) : undefined}
      >
        <TextInput
          id={linkId}
          value={flow.link}
          readOnly={flow.busy}
          spellCheck={false}
          onChange={(event) => {
            flow.edit(event.target.value);
          }}
          onBlur={() => {
            flow.leave();
          }}
        />
      </Field>
      <div role="status">
        {summary === null ? null : (
          <>
            <Notice>{`${summary.folderName}. ${summary.headline}`}</Notice>
            {summary.bullets.length === 0 ? null : <Notice>{summary.bullets.join(" · ")}</Notice>}
            {summary.closing === null ? null : <Notice>{summary.closing}</Notice>}
          </>
        )}
        {summary?.nothingToImport === true ? <Notice>{NOTHING_TO_IMPORT}</Notice> : null}
        {flow.failure === null ? null : <Notice role="alert">{flow.failure}</Notice>}
        {flow.saveNote ? <Notice role="alert">{SAVE_TOO_LONG}</Notice> : null}
      </div>
    </ConnectFrame>
  );
}
