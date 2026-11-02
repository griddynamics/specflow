"use client";
// Phase 1 Mission Control sources (owner redesign 2026-10-15; plans/connect-build-kg-button): the actions on the header row,
// left-aligned on the project dropdown's line. First the build button, then "Add source".
//  - The build button's label carries the state (BUILD KNOWLEDGE, BUILDING…, UPDATE KNOWLEDGE, ERROR); the detail is the tooltip and
//    the accessible description. Yellow only when a click starts something. Off and busy states use aria-disabled, so the button
//    keeps the focus (a disabled one drops it to <body>) and the tooltip shows on keyboard focus too. Busy spins (aria-hidden);
//    Error is red with an icon and opens the error details.
//  - "Add source" opens a menu: Google Drive, GitHub repository (only when the port offers it: hidden in production, K3), Local files (the file picker;
//    FR-SRC-0020: the only way to upload files, each pick makes one new batch). It is the yellow action while the
//    project has no source yet (the build button is off then), so the view keeps one yellow moment.
//  - Dialogs (add link, build confirm, build error) live in the screen, outside the header row.
import { useId, type Ref } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Menu } from "@/components/ui/Menu";
import { Tooltip } from "@/components/ui/Tooltip";
import type { ProjectSourcesStatus } from "@/lib/domain/sources";
import { buildButtonView, LOCAL_FILES, type BuildButtonView } from "./kb-logic";

/** The button's state for tests and styling: running, failed, ready (a click starts a build) or off. */
const stateOf = (v: BuildButtonView): "running" | "failed" | "ready" | "off" => {
  if (v.busy) return "running";
  if (v.failed) return "failed";
  return v.mode === null ? "off" : "ready";
};

/** Literal class names (the css-usage check reads them). Busy keeps its colour; Error is the red one. */
function buttonClass(v: BuildButtonView): string | undefined {
  if (v.busy) return "mcs-btn--busy";
  if (v.failed) return "mcs-btn--error";
  return v.tone === "off" ? "mcs-btn--off" : undefined;
}

export function HeaderActions({
  status,
  onOpenBuild,
  onError,
  pending = false,
  onAdd,
  onPickFiles,
  github = true,
  addSourceRef,
}: Readonly<{
  status: ProjectSourcesStatus;
  /** A click on Build / Update knowledge: the screen opens the confirm dialog of `mode`. */
  onOpenBuild: (mode: "first" | "update") => void;
  /** A click on the red Error button: the screen opens the error details. */
  onError: () => void;
  /** A build was confirmed and the status does not show it yet: the button is already busy. */
  pending?: boolean;
  onAdd: (kind: "gdrive" | "github") => void;
  onPickFiles: () => void;
  /** `SourcesPort.githubEnabled`. */
  github?: boolean;
  /** The "Add source" button, so the screen can return the focus to it. */
  addSourceRef?: Ref<HTMLButtonElement>;
}>) {
  const v = buildButtonView(status, new Date(), pending);
  const detailId = useId();
  const noSources = status.sources.length === 0;
  return (
    <div className="mcs-actions" data-testid="mcs-actions" data-phase={status.phase}>
      <Tooltip label={v.detail} side="bottom">
        <Button
          variant={v.tone === "primary" ? "primary" : "glass"}
          aria-disabled={v.tone === "off" ? true : undefined}
          className={buttonClass(v)}
          aria-describedby={detailId}
          onClick={() => {
            if (v.failed) onError();
            else if (v.mode) onOpenBuild(v.mode);
          }}
          data-testid="kb-build"
          data-mode={v.mode ?? undefined}
          data-state={stateOf(v)}
        >
          {v.busy ? <span className="gdg-spinner mcs-btn__spin" aria-hidden /> : null}
          {v.failed ? <Icon name="warning-circle" size={16} /> : null}
          {v.label}
        </Button>
      </Tooltip>
      <span id={detailId} className="mc-sr" data-testid="kb-detail">
        {v.detail}
      </span>
      <Menu
        label="Add source"
        items={[
          {
            label: "Google Drive",
            icon: "folder-open",
            description: "A folder you share with Specflow",
            onSelect: () => {
              onAdd("gdrive");
            },
          },
          ...(github
            ? [
                {
                  label: "GitHub repository",
                  icon: "git-branch" as const,
                  description: "Public, or private with a token",
                  onSelect: () => {
                    onAdd("github");
                  },
                },
              ]
            : []),
          {
            label: LOCAL_FILES,
            icon: "upload-simple",
            description: "Upload PDF, Office, HTML, Markdown or CSV files",
            onSelect: onPickFiles,
          },
        ]}
        trigger={({ open, toggle, id }) => (
          <Button
            variant={noSources ? "primary" : "glass"}
            aria-haspopup="menu"
            aria-expanded={open}
            aria-controls={id}
            onClick={toggle}
            ref={addSourceRef}
            data-testid="mcs-add-source"
          >
            Add source
          </Button>
        )}
      />
    </div>
  );
}
