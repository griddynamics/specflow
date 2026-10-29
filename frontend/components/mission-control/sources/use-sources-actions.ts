"use client";
// The actions of the sources screen that end in a toast: the failure toast, the "Knowledge built" notice, starting a build and
// uploading local files.
import { useCallback, useEffect, useRef, type RefObject } from "react";
import { useNotAvailable } from "@/components/shell/use-not-available";
import { useToast } from "@/components/ui/Toast";
import { isDataError } from "@/lib/domain/errors";
import type { BuildMode, KnowledgePort, ProjectSourcesStatus, SourcesPort } from "@/lib/domain/sources";
import { plural } from "./kb-logic";
import { pdfOnlyToast, splitUpload, startFailedToast, toastFor } from "./source-logic";

/** The danger toast of a failed action; production Build knowledge says "not available yet" instead (Q12, FR-GDUI-0021 note). */
export function useFailToast() {
  const toast = useToast();
  const notAvailable = useNotAvailable();
  return useCallback(
    (e: unknown) => {
      if (notAvailable.from(e)) return;
      const conflict = isDataError(e) && e.code === "conflict";
      toast({
        title: conflict ? "Already running" : "Something went wrong",
        message: conflict ? "Wait for the current run to finish." : "Try again.",
        tone: "danger",
      });
    },
    [toast, notAvailable],
  );
}

/** The first build just succeeded: say so. */
export function useKnowledgeBuiltToast(status: ProjectSourcesStatus | null) {
  const toast = useToast();
  const wasReady = useRef<boolean | null>(null);
  useEffect(() => {
    if (!status) return;
    if (wasReady.current === false && status.ready) {
      toast({
        title: "Knowledge built",
        message: `${plural(status.lastBuild?.documentCount ?? status.totals.documents, "document")} are in the project's knowledge.`,
        tone: "success",
      });
    }
    wasReady.current = status.ready;
  }, [status, toast]);
}

/** Starts a build; where no build exists (production) a click says so, in the app's own words. */
export function useBuild(knowledge: KnowledgePort, projectId: string, fail: (e: unknown) => void) {
  const toast = useToast();
  return (mode: BuildMode) =>
    knowledge.build(projectId, mode).catch((e: unknown) => {
      if (!knowledge.buildAvailable && isDataError(e) && e.code === "not_available") {
        toast({ title: "Not available yet", message: "Building knowledge is not available yet.", tone: "info" });
        return;
      }
      fail(e);
    });
}

/** Uploads the picked files (production reads PDF only: one toast names the rest); one pick is one batch (FR-SRC-0020). */
export function useUpload(sources: SourcesPort, projectId: string, fail: (e: unknown) => void, reload: RefObject<() => void>) {
  const toast = useToast();
  return async (picked: File[]) => {
    const { files, skipped } = splitUpload(picked, sources.pdfOnly);
    if (skipped.length) toast(pdfOnlyToast(skipped));
    if (files.length === 0) return;
    try {
      const report = await sources.upload(projectId, files);
      // FR-SRC-0020: no source was made when the start failed; the toast then names every file.
      toast(report.sourceId === null ? startFailedToast(files.map((f) => f.name)) : toastFor(report));
      reload.current();
    } catch (e) {
      fail(e);
    }
  };
}
