"use client";
// Phase 1 Mission Control sources (owner redesign 2026-10-15): the actions on the header row, left-aligned on the project dropdown's
// line. First the build button, then "Add source", then "⋯" (Rebuild from scratch) once something was built.
//  - The build button's label carries the state (BUILD KNOWLEDGE, BUILDING 18/42, UP TO DATE, UPDATE KNOWLEDGE); the detail is the
//    tooltip and the accessible description. Yellow only when a click starts something. Off states use aria-disabled, so the button
//    keeps the focus (a disabled one drops it to <body>) and the tooltip shows on keyboard focus too.
//  - "Add source" opens a menu: Google Drive, GitHub repository (only when the port offers it: hidden in production, K3), Local files (the file picker;
//    FR-SRC-0020: the only way to upload files, each pick makes one new batch). It is the yellow action while the
//    project has no source yet (the build button is off then), so the view keeps one yellow moment.
//  - Dialogs (add link, rebuild confirm) live in the screen, outside the header row.
import { useId, type Ref } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Menu } from "@/components/ui/Menu";
import { Tooltip } from "@/components/ui/Tooltip";
import type { BuildMode, ProjectSourcesStatus } from "@/lib/domain/sources";
import { buildButtonView, LOCAL_FILES, REBUILD_LABEL } from "./kb-logic";

export function HeaderActions({
  status,
  onBuild,
  onRebuild,
  onAdd,
  onPickFiles,
  github = true,
  pdfOnly = false,
  addSourceRef,
}: Readonly<{
  status: ProjectSourcesStatus;
  onBuild: (mode: BuildMode) => void;
  onRebuild: () => void;
  onAdd: (kind: "gdrive" | "github") => void;
  onPickFiles: () => void;
  /** `SourcesPort.githubEnabled`. */
  github?: boolean;
  /** `SourcesPort.pdfOnly`: the Local files text says so. */
  pdfOnly?: boolean;
  /** The "Add source" button, so the screen can return the focus to it. */
  addSourceRef?: Ref<HTMLButtonElement>;
}>) {
  const v = buildButtonView(status);
  const detailId = useId();
  const on = v.mode !== null;
  const noSources = status.sources.length === 0;
  return (
    <div className="mcs-actions" data-testid="mcs-actions" data-phase={status.phase}>
      <Tooltip label={v.detail} side="bottom">
        <Button
          variant={on ? "primary" : "glass"}
          aria-disabled={on ? undefined : true}
          className={on ? undefined : "mcs-btn--off"}
          aria-describedby={detailId}
          onClick={() => {
            if (v.mode) onBuild(v.mode);
          }}
          data-testid="kb-build"
          data-mode={v.mode ?? undefined}
        >
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
            description: pdfOnly ? "Upload PDF files" : "Upload PDF or Markdown files",
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
      {v.menu ? (
        <Menu
          label="Knowledge actions"
          trigger={({ open, toggle, id }) => (
            <button
              type="button"
              className="gdg-iconbtn"
              aria-label="More knowledge actions"
              aria-haspopup="menu"
              aria-expanded={open}
              aria-controls={id}
              onClick={toggle}
              data-testid="kb-menu"
            >
              <Icon name="dots-three" size={20} />
            </button>
          )}
          items={[
            {
              label: REBUILD_LABEL,
              icon: "arrow-counter-clockwise",
              description: "Process every document again",
              disabled: status.build !== null,
              onSelect: onRebuild,
            },
          ]}
        />
      ) : null}
    </div>
  );
}
