"use client";
// The files inside a source node (upload, Drive folder, Confluence link, Jira epic): the first page, then "Show more". It is the V1 reader
// (`use-source-files.ts`: paging, the 5 s refresh while something is still working), shown with the Graphico list, not the V1 rows.
import { Button, FileList, Notice, SectionHead } from "@graphico/ui";
import { useSourceFiles } from "@/components/mission-control/sources/use-source-files";
import type { SourceKind } from "@/lib/domain/sources";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { fileRowOf } from "./node-files-logic";

export function NodeFiles({
  projectId,
  sourceId,
  kind,
}: Readonly<{ projectId: string; sourceId: string; kind: Exclude<SourceKind, "github"> }>) {
  const { sources } = useV2Ports();
  const files = useSourceFiles(sources, projectId, sourceId);
  let body;
  if (files.phase === "loading") body = <Notice>{CANVAS_COPY.filesLoading}</Notice>;
  else if (files.phase === "error") {
    body = (
      <>
        <Notice role="alert">{CANVAS_COPY.filesFailed}</Notice>
        <div>
          <Button size="sm" variant="ghost" onClick={files.retry}>
            {CANVAS_COPY.filesRetry}
          </Button>
        </div>
      </>
    );
  } else if (files.items.length === 0) body = <Notice>{CANVAS_COPY.filesNone}</Notice>;
  else body = <FileList label={CANVAS_COPY.filesLabel} rows={files.items.map(fileRowOf)} />;
  return (
    <>
      <SectionHead title={CANVAS_COPY.filesTitle[kind]} />
      {body}
      {files.hasMore || files.moreFailed ? (
        <div>
          <Button size="sm" variant="ghost" disabled={files.loadingMore} onClick={files.loadMore}>
            {files.moreFailed ? CANVAS_COPY.filesRetry : CANVAS_COPY.filesMore}
          </Button>
        </div>
      ) : null}
    </>
  );
}
