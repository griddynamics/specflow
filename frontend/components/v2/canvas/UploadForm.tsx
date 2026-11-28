"use client";
// Upload: the files are picked or dropped here and go to the project as one batch (the V1 `useUpload`: unsupported types are named in one toast,
// the toast says what became of each file). The batch may be named first; blank keeps the default, which the node shows as "Upload 10:42".
// The dialog closes at once; the batch shows as a node when the status is read again.
import { useId, useState } from "react";
import { DropZone, Field, TextInput } from "@graphico/ui";
import { useFailToast, useUpload } from "@/components/shared/sources/use-sources-actions";
import { limitsText, UPLOAD_ACCEPT } from "@/lib/domain/upload-formats";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { DEFAULT_UPLOAD_NAME, uploadName } from "./nodes-sources";

export function UploadForm({ projectId, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const upload = useUpload(sources, projectId, useFailToast(), reload);
  const nameId = useId();
  const [name, setName] = useState("");
  // The default name the node would get now: the placeholder says what the batch is called when this stays empty.
  const [fallback] = useState(() => {
    const now = new Date();
    return uploadName({ addedAt: now.toISOString(), nameSuffix: 1, name: DEFAULT_UPLOAD_NAME }, now);
  });
  return (
    <ConnectFrame onBack={onBack}>
      <Field label={CANVAS_COPY.uploadName} htmlFor={nameId}>
        <TextInput
          id={nameId}
          value={name}
          placeholder={fallback}
          spellCheck={false}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <DropZone
        title={CANVAS_COPY.dropTitle}
        note={limitsText()}
        actionLabel={CANVAS_COPY.chooseFiles}
        accept={UPLOAD_ACCEPT}
        onFiles={(files) => {
          if (files.length === 0) return;
          onClose();
          void upload(files, name.trim());
        }}
      />
    </ConnectFrame>
  );
}
