"use client";
// The actions of the sources screen that end in a toast: the failure toast and uploading local files; and starting a build,
// whose failure shows on the status (the red Error button), not in a toast.
import { useCallback, type RefObject } from "react";
import { useNotAvailable } from "@/components/shared/shell/use-not-available";
import { useToast } from "@/components/shared/ui/Toast";
import { isDataError } from "@/lib/domain/errors";
import type { SourcesPort } from "@/lib/domain/sources";
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

/** Uploads the picked files (only the accepted types are sent: one toast names the rest); one pick is one batch (FR-SRC-0020). */
export function useUpload(sources: SourcesPort, projectId: string, fail: (e: unknown) => void, reload: RefObject<() => void>) {
  const toast = useToast();
  return async (picked: File[], name?: string) => {
    const { files, skipped } = splitUpload(picked);
    if (skipped.length) toast(unsupportedFilesToast(skipped));
    if (files.length === 0) return;
    try {
      const report = await sources.upload(projectId, files, name);
      // FR-SRC-0020: no source was made when the start failed; the toast then names every file.
      toast(report.sourceId === null ? startFailedToast(files.map((f) => f.name)) : toastFor(report));
      reload.current();
    } catch (e) {
      fail(e);
    }
  };
}
