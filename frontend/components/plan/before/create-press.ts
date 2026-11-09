// FE-PLAN-03.AC3/AC4 (P19-19, P22-3, P22-4): the pure logic of Stage 0, kept apart from React.
import { isDataError } from "@/lib/domain/errors";
import type { PlanPreview } from "@/lib/domain/plan";
import { COPY } from "@/lib/copy";
import type { ToastInput } from "@/components/ui/Toast";

/** The 409 answers of `POST /plan` that refuse a run (the API codes): each has its own info toast. */
const REFUSALS: Record<string, string> = {
  no_graph_build: "Build the knowledge graph before creating the plan.",
  graph_build_running: "The knowledge graph is being built. Create the plan when it is ready.",
};

const refusal = (e: unknown): string | undefined => {
  const code = isDataError(e) ? e.detail?.apiCode : undefined;
  return code !== undefined && Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : undefined;
};

/**
 * The press handler of "Create PLAN". The first press calls `start`; presses while it is pending are ignored (one run). A plain `conflict`
 * (a run already exists) is swallowed: the stage event moves the screen. Any other failure, the refusals of `REFUSALS` included, calls `onFail`
 * with the error (a toast) and the next press starts again. The guard clears when `start` settles (a ref only: no state update, safe after unmount).
 */
export function createPress(start: () => Promise<unknown>, onFail: (e: unknown) => void): () => Promise<void> {
  let pending = false;
  return async () => {
    if (pending) return;
    pending = true;
    try {
      await start();
    } catch (e) {
      if (!(isDataError(e) && e.code === "conflict" && refusal(e) === undefined)) onFail(e);
    } finally {
      pending = false;
    }
  };
}

/** The toast for a failed "Create PLAN": neutral when the port is not available yet or the API refused the run, else the danger toast. */
export function createFailToast(e: unknown): ToastInput {
  if (isDataError(e) && e.code === "not_available") return { title: COPY.planCreateUnavailable, tone: "info" };
  const refused = refusal(e);
  if (refused !== undefined) return { title: refused, tone: "info" };
  return { title: "Could not create the plan", message: "Try again.", tone: "danger" };
}

/** The table slot: a skeleton while loading, the note with "Try again" when the read failed, else the table (P22-4). */
export function tableView(t: PlanPreview | "failed" | null): "loading" | "failed" | "ready" {
  if (t === null) return "loading";
  return t === "failed" ? "failed" : "ready";
}
