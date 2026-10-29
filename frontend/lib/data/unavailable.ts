// SPL-DEMO-01, SPL-DEMO-02: the production adapter for the ports the backend does not serve yet (decisions, connectors, plan,
// assistant, files, comments, diagrams) and for the people directory and mentions. Each method answers "empty" or "not found",
// so a screen shows its existing empty state. No fixture, no fetch, no timer.
import type { AssistantEvent, AssistantIntro } from "@/lib/domain/assistant";
import { DataError } from "@/lib/domain/errors";
import type { PlanTable } from "@/lib/domain/plan";
import type { DataPorts } from "./ports";

export type UnavailablePorts = Pick<DataPorts, "files" | "comments" | "diagrams" | "decisions" | "assistant" | "connectors" | "plan">;

const notFound = (): Promise<never> => Promise.reject(new DataError("not_found", false));
const notAvailable = (): Promise<never> => Promise.reject(new DataError("not_available", false));
const noop = (): (() => void) => () => undefined;

/** An async iterable over fixed events (no timer, no fetch). */
function fromEvents(events: AssistantEvent[]): AsyncIterable<AssistantEvent> {
  return {
    [Symbol.asyncIterator]: () => {
      let i = 0;
      return {
        next: () => Promise.resolve(i < events.length ? { value: events[i++], done: false } : { value: undefined, done: true }),
      };
    },
  };
}

const EMPTY_TABLE: PlanTable = { rows: [], distinct: { critical: 0, high: 0, medium: 0, low: 0 }, unlinked: 0 };

/** SpecBot text while the assistant has no backend. The greeting is `Hi` here; the composition adds the signed-in first name. */
export const ASSISTANT_UNAVAILABLE = "SpecBot is not available yet.";

export function unavailableIntro(firstName: string): AssistantIntro {
  return {
    greeting: firstName === "" ? "Hi" : `Hi ${firstName}`,
    subline: ASSISTANT_UNAVAILABLE,
    placeholder: "SpecBot is not available yet",
    suggestions: [],
  };
}

export const unavailable: UnavailablePorts = {
  decisions: {
    list: () => Promise.resolve([]),
    domains: () => Promise.resolve([]),
    get: notFound,
    submit: notFound,
  },
  connectors: {
    list: () => Promise.resolve([]),
    categories: () => Promise.resolve(["All"]),
  },
  plan: {
    table: () => Promise.resolve({ ...EMPTY_TABLE, distinct: { ...EMPTY_TABLE.distinct } }),
    state: () => Promise.resolve({ phase: "idle" }),
    subscribe: noop,
    create: notAvailable,
    leave: () => undefined,
    updateTaskBody: notFound,
    develop: notFound,
  },
  assistant: {
    intro: () => Promise.resolve(unavailableIntro("")),
    ask: () => fromEvents([{ type: "answer", markdown: ASSISTANT_UNAVAILABLE }, { type: "done" }]),
  },
  files: {
    tree: notAvailable, // the Files screen shows "Files are not available yet" when the tree cannot load
    get: notFound,
    readme: notFound,
    updateBlock: notFound,
    addFiles: notAvailable,
    subscribe: noop,
  },
  comments: {
    list: () => Promise.resolve([]),
    create: notFound,
    reply: notFound,
    resolve: notFound,
    subscribe: noop,
  },
  diagrams: {
    business: notFound,
    architecture: notFound,
  },
};
