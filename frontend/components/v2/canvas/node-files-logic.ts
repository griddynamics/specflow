// What the node dialog shows of the files inside a source and of renaming an upload: pure, over the V1 words (`source-logic.ts`).
import { reasonText, STATE_LABEL } from "@/components/mission-control/sources/source-logic";
import { isDataError } from "@/lib/domain/errors";
import { CATEGORY_LABEL, type SourceFile } from "@/lib/domain/sources";
import type { FileRowView } from "@graphico/ui";
import { CANVAS_COPY } from "./canvas-copy";

/** "PDF", "Word documents", "Markdown": what kind of file it is. */
export const kindOf = (f: Pick<SourceFile, "category">): string => (f.category === "markdown" ? "Markdown" : CATEGORY_LABEL[f.category]);

/** One row: the name and kind always; the state word (tag) and the V1 reason (note) only for a file that is not done. */
export function fileRowOf(f: SourceFile): FileRowView {
  const reason = reasonText(f);
  return {
    key: f.id,
    name: f.name,
    format: kindOf(f),
    ...(f.state === "done" ? {} : { tag: STATE_LABEL[f.state] }),
    ...(reason === null ? {} : { note: reason }),
  };
}

/** What a refused rename says, in words: a bad name (422), a source that is not an upload (409), a source that is gone (404), anything else. */
export function renameProblem(e: unknown): string {
  if (!isDataError(e)) return CANVAS_COPY.renameFailed;
  if (e.code === "validation") return CANVAS_COPY.renameInvalid;
  if (e.detail?.apiCode === "source_not_renamable") return CANVAS_COPY.renameNot;
  return e.code === "not_found" && e.detail?.apiCode === "source_not_found" ? CANVAS_COPY.renameGone : CANVAS_COPY.renameFailed;
}
