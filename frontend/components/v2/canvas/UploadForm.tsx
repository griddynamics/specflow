"use client";
// Upload: the files are picked or dropped here and go to the project as one batch (the V1 `useUpload`: unsupported types are named in one toast,
// the toast says what became of each file). The dialog closes at once; the batch shows as a node when the status is read again.
import { DropZone } from "@graphico/ui";
import { useFailToast, useUpload } from "@/components/mission-control/sources/use-sources-actions";
import { limitsText, UPLOAD_ACCEPT } from "@/lib/domain/upload-formats";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

export function UploadForm({ projectId, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const upload = useUpload(sources, projectId, useFailToast(), reload);
  return (
    <ConnectFrame onBack={onBack}>
      <DropZone
        title={CANVAS_COPY.dropTitle}
        note={limitsText()}
        actionLabel={CANVAS_COPY.chooseFiles}
        accept={UPLOAD_ACCEPT}
        onFiles={(files) => {
          if (files.length === 0) return;
          onClose();
          void upload(files);
        }}
      />
    </ConnectFrame>
  );
}
