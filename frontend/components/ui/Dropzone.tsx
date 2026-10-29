"use client";
// FE-CP-06, FE-DS-02 (R5): port of `Dropzone` in bundle.js. Dashed glass area: drop files or use the button, which opens the
// system picker through a hidden file input (a script can set its files). The value is cleared after each pick so the
// same file can be chosen again.
import { useRef, useState, type DragEvent } from "react";
import { Button } from "./Button";
import { Icon, type IconName } from "./Icon";
import { cx, nonEmpty, truthy } from "./utils";

export interface DropzoneProps {
  onFiles?: (files: File[]) => void;
  title?: string;
  hint?: string;
  buttonLabel?: string;
  accept?: string;
  multiple?: boolean;
  primary?: boolean;
  compact?: boolean;
  icon?: IconName;
  className?: string;
}

export function Dropzone({
  onFiles,
  title,
  hint,
  buttonLabel,
  accept,
  multiple,
  primary,
  compact,
  icon,
  className,
}: Readonly<DropzoneProps>) {
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  // `accept` and the size hint are not enforced on drop (the picker filters by `accept`); the owner filters (P08).
  const files = (list: FileList | null) => {
    const picked = Array.from(list ?? []);
    if (picked.length) onFiles?.(picked);
  };
  return (
    <div
      role="presentation"
      className={cx("gdg-drop", over && "is-over", truthy(compact) && "gdg-drop--compact", className)}
      onDragOver={(e: DragEvent) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={(e) => {
        // moving over a child fires dragleave on the zone; only leaving the zone clears the mark
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(false);
      }}
      onDrop={(e: DragEvent) => {
        e.preventDefault();
        setOver(false);
        files(e.dataTransfer.files);
      }}
    >
      <span className="gdg-drop__icon">
        <Icon name={icon ?? "upload-simple"} size={truthy(compact) ? 20 : 28} />
      </span>
      <div className="gdg-drop__text">
        <b>{nonEmpty(title) ?? "Drop files here"}</b>
        <span>{nonEmpty(hint) ?? "Markdown, PDF or Word, up to 20 MB each"}</span>
      </div>
      <Button variant={truthy(primary) ? "primary" : "glass"} size="sm" onClick={() => input.current?.click()}>
        {nonEmpty(buttonLabel) ?? "Choose files"}
      </Button>
      <input
        ref={input}
        type="file"
        multiple={multiple !== false}
        accept={accept}
        hidden
        onChange={(e) => {
          files(e.target.files);
          e.target.value = "";
        }}
      />
    </div>
  );
}
