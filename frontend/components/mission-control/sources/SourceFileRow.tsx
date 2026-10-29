// FR-SRC-0010 (mockup "Details 3 — grouped"): one file row: a file-type icon, the name (cut with an ellipsis; the full name on hover
// through `title` and, on keyboard focus, by CSS that unfolds the name of the focused row), the state as a tag (Processing: a spinner and the word) and, for a problem file, its
// reason under the name. Only design-system glyphs exist, so the icon is a file, a Markdown file or a play mark (USAGE rule 7).
import { Icon } from "@/components/ui/Icon";
import { Tag, type TagProps } from "@/components/ui/Tag";
import type { FileState, SourceFile } from "@/lib/domain/sources";
import { fileIcon, reasonText, STATE_LABEL } from "./source-logic";
import { tabStop, truthy } from "@/components/ui/utils";

const TONE: Record<Exclude<FileState, "processing">, NonNullable<TagProps["tone"]>> = {
  waiting: "neutral",
  done: "success",
  not_read: "info",
  skipped: "warning",
  failed: "danger",
  already_in_project: "info",
};

/** `tab`: the row is the list's one Tab stop (roving focus, FileGroups); the other rows are reached with the arrow keys. */
export function SourceFileRow({ file, tab }: Readonly<{ file: SourceFile; tab: boolean }>) {
  const reason = reasonText(file);
  return (
    <li
      className="mcs-file"
      {...tabStop(tab)}
      aria-label={[file.name, STATE_LABEL[file.state], reason].filter(Boolean).join(". ")}
      data-testid="sd-file"
      data-id={file.id}
      data-state={file.state}
    >
      <Icon name={fileIcon(file.category)} size={20} />
      <div className="mcs-file__text">
        <p className="body-sm mcs-file__name" title={file.name} data-testid="sd-file-name">
          {file.name}
        </p>
        {truthy(reason) ? (
          <p className="caption mcs-file__why" data-testid="sd-file-reason">
            {reason}
          </p>
        ) : null}
      </div>
      {file.state === "processing" ? (
        <span className="caption mcs-file__busy">
          <span className="gdg-spinner mcs-file__spin" aria-hidden data-testid="sd-file-spinner" />
          {STATE_LABEL.processing}
        </span>
      ) : (
        <Tag tone={TONE[file.state]}>{STATE_LABEL[file.state]}</Tag>
      )}
    </li>
  );
}
