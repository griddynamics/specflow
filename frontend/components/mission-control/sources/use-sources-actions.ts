"use client";
// The actions of the sources screen that end in a toast: the failure toast, the "Knowledge built" notice and uploading local files;
// and starting a build, whose failure shows on the status (the red Error button), not in a toast.
import { useCallback, useEffect, useRef, type RefObject } from "react";
import { useNotAvailable } from "@/components/shell/use-not-available";
import { useToast } from "@/components/ui/Toast";
import { isDataError } from "@/lib/domain/errors";
import type { KnowledgePort, ProjectSourcesStatus, ReasoningLevel, SourcesPort } from "@/lib/domain/sources";
import { plural } from "./kb-logic";
import { splitUpload, startFailedToast, toastFor, unsupportedFilesToast } from "./source-logic";

/** The danger toast of a failed action (upload, sync); an action the port does not offer shows its own notice instead. */
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

/** The first build just succeeded: say so. Only after this screen saw it run: a reload, or a late first read, never toasts. */
export function useKnowledgeBuiltToast(status: ProjectSourcesStatus | null) {
  const toast = useToast();
  const wasReady = useRef<boolean | null>(null);
  const sawBuild = useRef(false);
  useEffect(() => {
    if (!status) return;
    if (status.build !== null) sawBuild.current = true;
    if (wasReady.current === false && status.ready && sawBuild.current) {
      sawBuild.current = false;
      toast({
        title: "Knowledge built",
        message: `${plural(status.totals.documents, "document")} are in the project's knowledge.`,
        tone: "success",
      });
    }
    wasReady.current = status.ready;
  }, [status, toast]);
}

/**
 * Starts a build with the chosen level. The rejection is swallowed on purpose: the port also puts the failure in the next status
 * (`buildFailure`), which turns the button into Error; a toast on top would say it twice.
 */
export function useBuild(knowledge: KnowledgePort, projectId: string) {
  return (level: ReasoningLevel): Promise<void> => knowledge.build(projectId, { reasoningLevel: level }).catch(() => undefined);
}

/** Uploads the picked files (only the accepted types are sent: one toast names the rest); one pick is one batch (FR-SRC-0020). */
export function useUpload(sources: SourcesPort, projectId: string, fail: (e: unknown) => void, reload: RefObject<() => void>) {
  const toast = useToast();
  return async (picked: File[]) => {
    const { files, skipped } = splitUpload(picked);
    if (skipped.length) toast(unsupportedFilesToast(skipped));
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
