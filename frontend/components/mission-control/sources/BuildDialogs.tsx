"use client";
// The two dialogs of the knowledge build (plans/connect-build-kg-button): the confirm with the reasoning-level slider, and the error
// details with copyable ids and codes. Which one is open is the screen's `Dialog` state.
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { RangeSlider } from "@/components/ui/RangeSlider";
import { Tooltip } from "@/components/ui/Tooltip";
import { truthy } from "@/components/ui/utils";
import { isReasoningLevel, type BuildFailure, type ReasoningLevel } from "@/lib/domain/sources";
import { COPIED_MS, COPY_FALLBACK_HINT } from "./drive-logic";
import { buildErrorReport, estimateText, plural, REASONING_LEVELS } from "./kb-logic";
import { ConfirmDialog } from "./SourceDialogs";

const DEFAULT_LEVEL: ReasoningLevel = 2; // Medium (D8)
const LEVEL_NOTE = "The level affects how long the build takes.";

/** Every Build / Update click confirms first. The level (default Medium) is sent with the build; the estimate is the slider's hint. */
export function BuildConfirmDialog(
  props: Readonly<{ mode: "first" | "update"; documents: number; onConfirm: (level: ReasoningLevel) => void; onClose: () => void }>,
) {
  const { mode, documents, onConfirm } = props;
  const [level, setLevel] = useState<ReasoningLevel>(DEFAULT_LEVEL);
  const first = mode === "first";
  const hint = first
    ? `${REASONING_LEVELS[level - 1]}: this can take ${estimateText(documents, level)} for ${plural(documents, "document")}. ${LEVEL_NOTE}`
    : LEVEL_NOTE;
  return (
    <ConfirmDialog
      title={first ? "Build knowledge?" : "Update knowledge?"}
      description={
        first
          ? "Run it when all of the project's files are connected and imported."
          : "Only new, changed and removed documents are processed. This usually takes a few minutes."
      }
      confirmLabel={first ? "Build" : "Update"}
      onConfirm={() => {
        onConfirm(level);
      }}
      onClose={props.onClose}
      testId="kb-build-dialog"
    >
      <RangeSlider
        label="Reasoning level"
        options={REASONING_LEVELS}
        value={level}
        onChange={(v) => {
          if (isReasoningLevel(v)) setLevel(v);
        }}
        hint={hint}
        testId="kb-level"
      />
    </ConfirmDialog>
  );
}

type CopyState = "idle" | "copied" | "manual";

/** The copy control of the report; when the browser refuses, the text is selected so the user can copy it by hand (as the Drive modal). */
function useCopyText(text: string) {
  const [copy, setCopy] = useState<CopyState>("idle");
  const textEl = useRef<HTMLPreElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(
    () => () => {
      clearTimeout(timer.current);
    },
    [],
  );
  async function copyText() {
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(text);
      setCopy("copied");
      timer.current = setTimeout(() => {
        setCopy("idle");
      }, COPIED_MS);
    } catch {
      const el = textEl.current;
      const sel = window.getSelection();
      if (el && sel) {
        const range = document.createRange();
        range.selectNodeContents(el);
        sel.removeAllRanges();
        sel.addRange(range);
      }
      setCopy("manual");
    }
  }
  return { copy, textEl, copyText };
}

/**
 * The details of a failed build: ids, codes and times only, to copy and send to the Specflow team. "Try again" opens the confirm
 * dialog (`onRetry`); it is aria-disabled with the reason in `blocked` (the import lock, no documents yet). The page address is
 * read once when the dialog opens, without query or hash.
 */
export function BuildErrorDialog({
  projectId,
  failure,
  blocked,
  onRetry,
  onClose,
}: Readonly<{ projectId: string; failure: BuildFailure; blocked: string | null; onRetry: () => void; onClose: () => void }>) {
  const [page] = useState(() => window.location.origin + window.location.pathname);
  const report = buildErrorReport(projectId, failure, page);
  const { copy, textEl, copyText } = useCopyText(report);
  const retry = (
    <Button
      aria-disabled={truthy(blocked) ? true : undefined}
      aria-describedby={truthy(blocked) ? "kb-error-blocked" : undefined}
      onClick={() => {
        if (!truthy(blocked)) onRetry();
      }}
      data-testid="kb-error-retry"
    >
      Try again
    </Button>
  );
  return (
    <Modal
      title="Knowledge build failed"
      description="Copy the details and send them to the Specflow team."
      size="md"
      onClose={onClose}
      footer={
        <>
          <Button variant="glass" onClick={onClose} data-testid="kb-error-close">
            Close
          </Button>
          <Button variant="glass" onClick={() => void copyText()} data-testid="kb-error-copy">
            {copy === "copied" ? "Copied" : "Copy details"}
          </Button>
          {truthy(blocked) ? <Tooltip label={blocked}>{retry}</Tooltip> : retry}
        </>
      }
    >
      <div data-testid="kb-error-dialog">
        <pre ref={textEl} className="mcs-build-error__text" data-testid="kb-error-text">
          {report}
        </pre>
        {copy === "manual" ? <p className="caption mcs-build-error__hint">{COPY_FALLBACK_HINT}</p> : null}
        {truthy(blocked) ? (
          <span className="mc-sr" id="kb-error-blocked" data-testid="kb-error-blocked">
            {blocked}
          </span>
        ) : null}
        <span className="mc-sr" role="status" data-testid="kb-error-copy-status">
          {copy === "copied" ? "Copied" : ""}
        </span>
      </div>
    </Modal>
  );
}
