// docs/projects/ui.md#general-tab: the rules behind `useEmailSuggestions`: nothing under 3 characters, a 200 ms pause, only the newest
// answer counts, a failed lookup is an empty list, dispose silences everything. Driven through `createSuggester` with fake timers.
import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { SUGGEST_DEBOUNCE_MS, createSuggester, suggestionQuery } from "@/components/members/use-email-suggestions";
import type { Suggestion } from "@/lib/domain/members";

const person = (n: string): Suggestion => ({ name: n, email: `${n}@example.test` });
const tick = async (ms: number) => {
  mock.timers.tick(ms);
  for (let i = 0; i < 5; i++) await Promise.resolve(); // let the promise chain settle
};

function setup(suggest: (q: string) => Promise<Suggestion[]>) {
  mock.timers.enable({ apis: ["setTimeout"] });
  const seen: Suggestion[][] = [];
  const s = createSuggester({ suggest, onItems: (items) => seen.push(items) });
  return { s, seen };
}

test("suggestionQuery: trimmed text of at least 3 code points, else null", () => {
  assert.equal(suggestionQuery("al"), null);
  assert.equal(suggestionQuery("  al  "), null);
  assert.equal(suggestionQuery(" ali "), "ali");
  assert.equal(suggestionQuery("😀😀"), null);
  assert.equal(suggestionQuery("😀😀😀"), "😀😀😀");
});

test("under 3 characters: no request, an empty list at once", async () => {
  const calls: string[] = [];
  const { s, seen } = setup((q) => {
    calls.push(q);
    return Promise.resolve([]);
  });
  s.query("al");
  await tick(1000);
  assert.deepEqual(calls, []);
  assert.deepEqual(seen, [[]]);
  s.dispose();
  mock.timers.reset();
});

test("the lookup waits 200 ms after the last keystroke; typing again restarts the pause", async () => {
  const calls: string[] = [];
  const { s, seen } = setup((q) => {
    calls.push(q);
    return Promise.resolve([person(q)]);
  });
  s.query("ali");
  await tick(SUGGEST_DEBOUNCE_MS - 1);
  assert.deepEqual(calls, []);
  s.query("alic");
  await tick(SUGGEST_DEBOUNCE_MS - 1);
  assert.deepEqual(calls, [], "the pause restarted");
  await tick(1);
  assert.deepEqual(calls, ["alic"]);
  assert.deepEqual(seen, [[person("alic")]]);
  s.dispose();
  mock.timers.reset();
});

test("a stale answer (an older query) is dropped, whichever order they come back in", async () => {
  const waiting = new Map<string, (items: Suggestion[]) => void>();
  const { s, seen } = setup((q) => new Promise((resolve) => waiting.set(q, resolve)));
  s.query("ali");
  await tick(SUGGEST_DEBOUNCE_MS);
  s.query("alic");
  await tick(SUGGEST_DEBOUNCE_MS);
  waiting.get("alic")?.([person("alice")]);
  await tick(0);
  waiting.get("ali")?.([person("old")]);
  await tick(0);
  assert.deepEqual(seen, [[person("alice")]]);
  s.dispose();
  mock.timers.reset();
});

test("shortening the text below 3 characters drops an answer still on its way", async () => {
  let resolve: ((items: Suggestion[]) => void) | undefined;
  const { s, seen } = setup(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  s.query("ali");
  await tick(SUGGEST_DEBOUNCE_MS);
  s.query("al");
  resolve?.([person("late")]);
  await tick(0);
  assert.deepEqual(seen, [[]]);
  s.dispose();
  mock.timers.reset();
});

test("a failed lookup is an empty list; dispose cancels the timer and silences a late answer", async () => {
  const failing = setup(() => Promise.reject(new Error("down")));
  failing.s.query("ali");
  await tick(SUGGEST_DEBOUNCE_MS);
  assert.deepEqual(failing.seen, [[]]);
  failing.s.dispose();
  mock.timers.reset();

  let resolve: ((items: Suggestion[]) => void) | undefined;
  let called = 0;
  const { s, seen } = setup(() => {
    called++;
    return new Promise((r) => {
      resolve = r;
    });
  });
  s.query("ali");
  s.dispose();
  await tick(1000);
  assert.equal(called, 0, "the pending timer was cleared");
  s.query("bob");
  await tick(SUGGEST_DEBOUNCE_MS);
  s.dispose();
  resolve?.([person("late")]);
  await tick(0);
  assert.deepEqual(seen, [], "nothing was shown");
  mock.timers.reset();
});

test("useEmailSuggestions renders inside the data provider and starts with no suggestions", async () => {
  const { stubCss } = await import("../support/css-stub.mjs");
  stubCss();
  const { createElement } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const { DataProvider } = await import("../../lib/data/provider");
  const { useEmailSuggestions } = await import("../../components/members/use-email-suggestions");
  const Probe = () => createElement("p", null, `items:${String(useEmailSuggestions("ali", "p1").items.length)}`);
  assert.match(renderToStaticMarkup(createElement(DataProvider, null, createElement(Probe))), /items:0/);
});
