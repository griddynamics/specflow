"use client";
// The files under a tested Drive folder, all checked, as the Jira epic lists its issues: a flat list with quiet Select all / Select none, the
// folder path after each name (none for a file in the root) and a status line. The selection is the ids left out (`pick-logic.ts`).
import { useState } from "react";
import { PickList, PickRow } from "@graphico/ui";
import type { DriveFile, DriveTestResult } from "@/lib/domain/sources";
import { CANVAS_COPY } from "./canvas-copy";
import { folderHint, pickableFiles, selectionText } from "./drive-pick-logic";
import { checkAll, checkNone, pickedCount, toggle, type Unchecked } from "./pick-logic";

/** The selection of a checked folder's files. It belongs to the folder check it was made on (like the Jira epic's): a changed link is a new check
 * and starts with every file checked, and a change of the selection never touches the check. `nothingPicked`: files are listed and none is checked. */
export function useFilePicks(tested: Extract<DriveTestResult, { ok: true }> | null) {
  const [left, setLeft] = useState<{ tested: object | null; ids: Unchecked }>({ tested: null, ids: checkAll() });
  const unchecked = left.tested === tested ? left.ids : checkAll();
  const files = tested === null ? [] : pickableFiles(tested);
  return {
    files,
    unchecked,
    nothingPicked: files.length > 0 && pickedCount(files, unchecked) === 0,
    pick: (ids: Unchecked) => {
      setLeft({ tested, ids });
    },
  };
}

export function DriveFileList({
  files,
  unchecked,
  capped,
  busy,
  onPick,
}: Readonly<{ files: readonly DriveFile[]; unchecked: Unchecked; capped: boolean; busy: boolean; onPick: (ids: Unchecked) => void }>) {
  return (
    <PickList
      legend={CANVAS_COPY.driveFiles}
      scroll
      notes={[selectionText(files, unchecked), ...(capped ? [CANVAS_COPY.driveCapped] : [])]}
      tools={[
        {
          label: CANVAS_COPY.driveSelectAll,
          disabled: unchecked.size === 0 || busy,
          onClick: () => {
            onPick(checkAll());
          },
        },
        {
          label: CANVAS_COPY.driveSelectNone,
          disabled: unchecked.size === files.length || busy,
          onClick: () => {
            onPick(checkNone(files));
          },
        },
      ]}
    >
      {files.map((file) => (
        <PickRow
          key={file.id}
          text
          label={file.name}
          hint={folderHint(file)}
          checked={!unchecked.has(file.id)}
          disabled={busy}
          onChange={(on) => {
            onPick(toggle(unchecked, file.id, on));
          }}
        />
      ))}
    </PickList>
  );
}
