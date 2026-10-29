"use client";
// FR-SRC-0009, 0012, 0025 (INT-SRC-0002): the file list of one source for the details modal, as React state over the reader in
// source-files-reader.ts (paging, the 5 s refresh, errors). The reader stops, with its timer, when the modal closes or the source changes.
import { useCallback, useEffect, useRef, useState } from "react";
import type { SourcesPort } from "@/lib/domain/sources";
import { INITIAL, startFileReader, type FileReader, type FilesState } from "./source-files-reader";

export type SourceFiles = FilesState & {
  loadMore: () => void;
  /** After a failed first read. */
  retry: () => void;
};

export function useSourceFiles(port: SourcesPort, projectId: string, sourceId: string): SourceFiles {
  const [state, setState] = useState<FilesState>(INITIAL);
  const [attempt, setAttempt] = useState(0);
  const reader = useRef<FileReader | null>(null);

  useEffect(() => {
    const r = startFileReader(port, projectId, sourceId, setState);
    reader.current = r;
    return () => {
      r.stop();
      if (reader.current === r) reader.current = null;
    };
  }, [port, projectId, sourceId, attempt]);

  const loadMore = useCallback(() => reader.current?.loadMore(), []);
  const retry = useCallback(() => {
    setAttempt((n) => n + 1);
  }, []);
  return { ...state, loadMore, retry };
}
