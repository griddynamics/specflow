"use client";
// One read of a port that the canvas shows: loaded at mount, read again when asked (`reload`) and, while `pollMs` says so, every few seconds.
// A read that fails keeps what was shown (null before the first success): the canvas shows what it knows and invents nothing.
import { useCallback, useEffect, useRef, useState } from "react";
import { startPolling } from "@/components/settings/github-logic";

export function usePolledRead<T>(
  load: () => Promise<T>,
  pollMs: (value: T | null) => number | null,
): { value: T | null; reload: () => void } {
  const [value, setValue] = useState<T | null>(null);
  const alive = useRef(true);
  const read = useCallback(() => {
    load().then(
      (next) => {
        if (alive.current) setValue(next);
      },
      () => undefined,
    );
  }, [load]);
  useEffect(() => {
    alive.current = true;
    read();
    return () => {
      alive.current = false;
    };
  }, [read]);
  const ms = pollMs(value);
  useEffect(() => (ms === null ? undefined : startPolling(true, read, ms)), [ms, read]);
  return { value, reload: read };
}
