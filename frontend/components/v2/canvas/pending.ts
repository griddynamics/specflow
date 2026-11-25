// The rules of a start that the read side has not shown yet (a build or a plan just started here): when it ends, and the timeout that ends it
// if nothing else does. Pure, so they are tested without a screen.

/** How long the hub looks busy on its own before the status is trusted again (the start may have failed). */
export const PENDING_MS = 15_000;

/** A start ends when the work is seen running, or when the state's signal moved (it ended before it was seen). */
export function clearsPending(mark: string | null, running: boolean, signal: string): boolean {
  return mark !== null && (running || signal !== mark);
}

/** Calls `onExpire` after `ms`; the returned function cancels it. */
export function startPendingTimer(onExpire: () => void, ms: number = PENDING_MS): () => void {
  const timer = setTimeout(onExpire, ms);
  return () => {
    clearTimeout(timer);
  };
}
