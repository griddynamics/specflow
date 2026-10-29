// A function with a fixed identity that always calls the latest `fn` (the ref is updated after every render and only read when the
// returned function is called), for objects that must keep their identity but have to see the newest callback at call time.
import { useCallback, useEffect, useRef } from "react";

export function useStableCallback<Args extends unknown[], Result>(fn: (...args: Args) => Result): (...args: Args) => Result {
  const latest = useRef(fn);
  useEffect(() => {
    latest.current = fn;
  });
  return useCallback((...args: Args) => latest.current(...args), []);
}
