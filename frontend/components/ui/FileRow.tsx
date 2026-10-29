// FE-CP-06, FE-DS-02 (R5): port of `FileRow` in bundle.js. One file in an upload list: type icon, name, size, state.
// Pure (no hooks): the remove handler comes from props.
import { Icon, type IconName } from "./Icon";
import { ProgressBar } from "./ProgressBar";
import { Tag } from "./Tag";
import { formatFileSize } from "./app-logic";
import { cx, nonEmpty } from "./utils";

export interface FileRowProps {
  name: string;
  size?: number | string;
  status?: "ready" | "uploading" | "processing" | "done" | "error";
  progress?: number;
  onRemove?: () => void;
  icon?: IconName;
  doneText?: string;
  errorText?: string;
}

/** The tag on the right of a row: the done and error words, or "Processing"; none for the other states. */
function statusTag(status: NonNullable<FileRowProps["status"]>, doneText: string | undefined, errorText: string | undefined) {
  if (status === "done") return <Tag tone="success">{nonEmpty(doneText) ?? "Uploaded"}</Tag>;
  if (status === "error") return <Tag tone="danger">{nonEmpty(errorText) ?? "Failed"}</Tag>;
  return status === "processing" ? <Tag tone="pending">Processing</Tag> : null;
}

export function FileRow({ name, size, status = "ready", progress, onRemove, icon, doneText, errorText }: Readonly<FileRowProps>) {
  const meta = statusTag(status, doneText, errorText);
  return (
    <div className={cx("gdg-filerow", `is-${status}`)}>
      <span className="gdg-filerow__icon">
        <Icon name={icon ?? (/\.md$/i.test(name) ? "file-md" : "file-text")} size={20} />
      </span>
      <div className="gdg-filerow__main">
        <div className="gdg-filerow__top">
          <span className="gdg-filerow__name">{name}</span>
          <span className="gdg-filerow__size">{formatFileSize(size)}</span>
        </div>
        {status === "uploading" ? <ProgressBar value={progress} /> : null}
      </div>
      {meta}
      {onRemove && (status === "ready" || status === "error") ? (
        <button type="button" className="gdg-iconbtn" aria-label={`Remove ${name}`} onClick={onRemove}>
          <Icon name="x" size={16} />
        </button>
      ) : null}
    </div>
  );
}
