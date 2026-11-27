"use client";
// Google Drive source modal (FR-GDUI-0001..0020, NFR-USE-0001, NFR-PORT-0001, NFR-SEC-0001). "Connect a Google Drive folder": the
// account email with a copy control, the collapsed four-step guide, one link field, and one primary action that is "Test connection"
// until the connection check succeeds for the link, then "Save". The pasted link goes only to `SourcesPort` (never fetched here). The component
// mounts when the modal opens and unmounts when it closes, so every opening starts empty (FR-GDUI-0001.AC3, FR-GDUI-0006.AC2).
import Image from "next/image";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/shared/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { TextField } from "@/components/ui/TextField";
import type { SourcesPort } from "@/lib/domain/sources";
import {
  COPIED_MS,
  COPY_FALLBACK_HINT,
  driveReasonMessage,
  type DriveSummary,
  emailLabel,
  GUIDE_STEPS,
  GUIDE_TOGGLE,
  NOTHING_TO_IMPORT,
  SAVE_TOO_LONG,
} from "@/components/shared/sources/drive-logic";
import { useDriveFlow } from "@/components/shared/sources/use-drive-flow";
// The four Google Drive screenshots (FR-GDUI-0007) are bundled, not served from /public: the sign-in proxy gates every path outside
// /_next, so a public file would reach a signed-out preview as the login page.
import shot1 from "./assets/1-share-with-us.png";
import shot2 from "./assets/2-add-specflow-sa.png";
import shot3 from "./assets/3-confirm-outside-user.png";
import shot4 from "./assets/4-copy-link.png";
import { truthy } from "@/components/shared/ui/utils";
import { COPY } from "@/lib/copy";

const SHOTS: Record<string, { src: string; width: number; height: number }> = {
  "1-share-with-us.png": shot1,
  "2-add-specflow-sa.png": shot2,
  "3-confirm-outside-user.png": shot3,
  "4-copy-link.png": shot4,
};

type CopyState = "idle" | "copied" | "manual";

function primaryLabel(canSave: boolean, saving: boolean, phase: "idle" | "testing" | "saving"): string {
  if (canSave) return saving ? "Saving…" : "Save";
  return phase === "testing" ? "Testing…" : "Test connection";
}

function copyStatus(copy: CopyState): string {
  if (copy === "copied") return "Copied";
  return copy === "manual" ? COPY_FALLBACK_HINT : "";
}

/** FR-GDUI-0003: the copy control of the account email; when the browser refuses, the text is selected so the user can copy it by hand. */
function useCopyEmail(email: string | null) {
  const [copy, setCopy] = useState<CopyState>("idle");
  const emailEl = useRef<HTMLSpanElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(
    () => () => {
      clearTimeout(timer.current);
    },
    [],
  );
  async function copyEmail() {
    if (!truthy(email)) return;
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(email);
      setCopy("copied");
      timer.current = setTimeout(() => {
        setCopy("idle");
      }, COPIED_MS);
    } catch {
      const el = emailEl.current;
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
  return { copy, emailEl, copyEmail };
}

/** FR-GDUI-0007: the four-step guide with its screenshots; `{email}` in a step is the Drive account email. */
function DriveGuide({ id, open, emailText }: Readonly<{ id: string; open: boolean; emailText: string }>) {
  if (!open) return null;
  return (
    <ol className="mcs-drive__steps" id={id} data-testid="drive-guide">
      {GUIDE_STEPS.map((step, i) => {
        const [before, after] = step.text.split("{email}");
        const alt = step.text.replace("{email}", emailText);
        return (
          <li key={step.image} className="mcs-drive__step">
            <p className="body-sm">
              {before}
              {step.text.includes("{email}") ? (
                <>
                  <span className="mcs-drive__email" data-testid="drive-guide-email">
                    {emailText}
                  </span>
                  {after}
                </>
              ) : null}
            </p>
            {/* The bundled file as it is (no optimiser round trip) and loaded at once, like a plain <img>: the guide shows it as
                soon as it expands. */}
            <Image
              src={SHOTS[step.image].src}
              width={SHOTS[step.image].width}
              height={SHOTS[step.image].height}
              alt={alt}
              className="mcs-drive__shot"
              data-testid={`drive-step-${String(i + 1)}-image`}
              unoptimized
              loading="eager"
            />
          </li>
        );
      })}
    </ol>
  );
}

/** The live region under the link field: the connection summary, the failure, or the "saving took too long" note. */
function DriveResult({
  summary,
  failure,
  saveNote,
}: Readonly<{ summary: DriveSummary | null; failure: string | null; saveNote: boolean }>) {
  return (
    <div className="mcs-drive__result" role="status" data-testid="drive-result">
      {summary ? (
        <div className="mcs-drive__summary" data-testid="drive-summary">
          <p className="headline mcs-drive__folder">{summary.folderName}</p>
          <p className="body">{summary.headline}</p>
          {summary.bullets.length ? (
            <ul className="mcs-drive__bullets">
              {summary.bullets.map((b) => (
                <li key={b} className="body-sm">
                  {b}
                </li>
              ))}
            </ul>
          ) : null}
          {truthy(summary.closing) ? <p className="body-sm">{summary.closing}</p> : null}
          {summary.nothingToImport ? <p className="body">{NOTHING_TO_IMPORT}</p> : null}
        </div>
      ) : null}
      {truthy(failure) ? (
        <p className="body" data-testid="drive-failure">
          {failure}
        </p>
      ) : null}
      {saveNote ? (
        <p className="body" data-testid="drive-save-note">
          {SAVE_TOO_LONG}
        </p>
      ) : null}
    </div>
  );
}

export function DriveDialog({
  projectId,
  sources,
  onSaved,
  onClose,
}: Readonly<{
  projectId: string;
  sources: SourcesPort;
  onSaved: () => void;
  onClose: () => void;
}>) {
  const ids = useId();
  const linkId = `${ids}-link`;
  const guideId = `${ids}-guide`;
  const flow = useDriveFlow({ projectId, sources, onSaved });
  const { identity, email, link, badLink, phase, busy, saving, saveNote, summary, canSave, blocked, failure, act } = flow;
  const [guide, setGuide] = useState(false);
  const linkField = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // The modal's own focus rule picks the first control; the link field is the one the user needs (FR-GDUI-0001.AC2).
    linkField.current?.querySelector("input")?.focus();
  }, []);

  const { copy, emailEl, copyEmail } = useCopyEmail(email);

  // FR-GDUI-0020: Esc, Cancel and the close control close at any time before a save succeeds, except while a save runs.
  const close = useCallback(() => {
    if (saving) return;
    onClose();
  }, [saving, onClose]);

  const emailText = emailLabel(identity);

  return (
    <Modal
      title="Connect a Google Drive folder"
      onClose={close}
      closeDisabled={saving}
      footer={
        <>
          <Button
            variant="glass"
            onClick={close}
            aria-disabled={saving ? true : undefined}
            className={saving ? "mcs-btn--off" : undefined}
            data-testid="drive-cancel"
          >
            Cancel
          </Button>
          <Button
            onClick={() => {
              if (!blocked) void act();
            }}
            aria-disabled={blocked ? true : undefined}
            aria-busy={busy || undefined}
            className={blocked ? "mcs-btn--off" : undefined}
            data-testid="drive-primary"
          >
            {primaryLabel(canSave, saving, phase)}
          </Button>
        </>
      }
    >
      <div className="mcs-drive" data-testid="mcs-drive-dialog">
        <div className="mcs-drive__share">
          <p className="body mcs-drive__account">
            Share the folder with this account as Viewer:{" "}
            <span ref={emailEl} className="mcs-drive__email" data-testid="drive-email">
              {emailText}
            </span>
          </p>
          <Button
            variant="glass"
            size="sm"
            onClick={() => void copyEmail()}
            disabled={!truthy(email)}
            aria-label={copy === "copied" ? "Copied" : "Copy account email"}
            data-testid="drive-copy"
          >
            {copy === "copied" ? "Copied" : "Copy"}
          </Button>
        </div>
        {copy === "manual" ? <p className="caption mcs-drive__hint">{COPY_FALLBACK_HINT}</p> : null}
        <span className="mc-sr" role="status" data-testid="drive-copy-status">
          {copyStatus(copy)}
        </span>

        <div className="mcs-drive__guide">
          <Button
            variant="plain"
            size="sm"
            onClick={() => {
              setGuide((g) => !g);
            }}
            aria-expanded={guide}
            aria-controls={guideId}
            data-testid="drive-guide-toggle"
          >
            <Icon name={guide ? "caret-down" : "caret-right"} size={14} />
            {GUIDE_TOGGLE}
          </Button>
          <DriveGuide id={guideId} open={guide} emailText={emailText} />
        </div>

        <div ref={linkField}>
          <TextField
            id={linkId}
            label="Google Drive folder link"
            value={link}
            placeholder={COPY.driveFolderLink}
            readOnly={busy}
            spellCheck={false}
            error={badLink ? driveReasonMessage("invalid_link", email) : undefined}
            onChange={(e) => {
              flow.edit(e.target.value);
            }}
            onBlur={() => {
              flow.leave();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                e.preventDefault();
                if (!blocked) void act();
              }
            }}
            data-testid="drive-link"
          />
        </div>

        <DriveResult summary={summary} failure={failure} saveNote={saveNote} />
      </div>
    </Modal>
  );
}
