// FE-PLAN-03.AC3/AC4 (P19-19, P22-3, P22-4): the pure logic of Stage 0, kept apart from React.
import { isDataError } from "@/lib/domain/errors";
import type { PlanTable } from "@/lib/domain/plan";
import { COPY } from "@/lib/copy";
import type { ToastInput } from "@/components/ui/Toast";

/**
 * The press handler of "Create PLAN". The first press calls `start`; presses while it is pending are ignored (one run). `conflict` (a run or
 * plan already exists) is swallowed: the stage event moves the screen. Any other failure calls `onFail` with the error (danger toast) and the next press
 * starts again. The guard clears when `start` settles (a ref only: no state update, safe after unmount).
 */
export function createPress(start: () => Promise<unknown>, onFail: (e: unknown) => void): () => Promise<void> {
  let pending = false;
  return async () => {
    if (pending) return;
    pending = true;
    try {
      await start();
    } catch (e) {
      if (!(isDataError(e) && e.code === "conflict")) onFail(e);
    } finally {
      pending = false;
    }
  };
}

/** The toast for a failed "Create PLAN": neutral when the port is not available yet, else the danger toast. */
export function createFailToast(e: unknown): ToastInput {
  if (isDataError(e) && e.code === "not_available") return { title: COPY.planCreateUnavailable, tone: "info" };
  return { title: "Could not create the plan", message: "Try again.", tone: "danger" };
}

/** The table slot: a skeleton while loading, the note with "Try again" when the read failed, else the table (P22-4). */
export function tableView(t: PlanTable | "failed" | null): "loading" | "failed" | "ready" {
  if (t === null) return "loading";
  return t === "failed" ? "failed" : "ready";
}
