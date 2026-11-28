// docs/jira-sync/ui.md#jira-tab (FR-JIRA-UI-01..05), the click flow without a browser: the Jira tab's state machine (`tabReducer`) driven
// through check -> save -> switch, the stale-answer guard, `busy` clearing on every answer, and the 5 s polling with fake timers.
// Sequence of the tested flow (the tab dispatches, the server answers):
//   owner -> tab: type keys      tab: edit (run 1)
//   owner -> tab: Check access   tab: start check (run 2) -> server -> checked(run 2): outcome ok, button "Save"
//   owner -> tab: Save           tab: start save (run 3)  -> server -> saved(run 3): settings = server answer, outcome cleared
//   owner -> tab: Switch         tab: start switch (run 4) -> server -> switched: enabled = server answer
import assert from "node:assert/strict";
import { mock, test } from "node:test";
import { DataError } from "@/lib/domain/errors";
import type { JiraSettings } from "@/lib/domain/jira";
import {
  failureOf,
  INITIAL_TAB,
  isRunning,
  isSavedTarget,
  phaseOf,
  POLL_MS,
  startPolling,
  tabReducer,
  targetOf,
  type Busy,
  type TabAction,
  type TabState,
} from "@/components/shared/settings/jira-logic";

const NONE: JiraSettings = {
  available: true,
  editable: true,
  siteUrl: "https://example.atlassian.net",
  jiraProject: null,
  epicKey: null,
  createdEpicKey: null,
  enabled: false,
  sync: null,
  connection: null,
};
const TOKEN = { mode: "token", connected: false, status: null, accountName: null, connectedAt: null, lastUsedAt: null } as const;
const OAUTH = { ...TOKEN, mode: "oauth" } as const;
const SAVED: JiraSettings = { ...NONE, jiraProject: "PAY", sync: { status: "idle", finishedAt: null, errorCode: null, progress: null } };
const OK = { result: "ok", projectName: "Payments platform", epicSummary: null } as const;
const apply = (st: TabState, ...actions: TabAction[]): TabState => actions.reduce(tabReducer, st);
const loaded = (s: JiraSettings = NONE): TabState => apply(INITIAL_TAB, { type: "loaded", settings: s, atlassian: TOKEN });
const phase = (st: TabState) => phaseOf(st.outcome, st.busy === "switch" ? null : st.busy);
const target = targetOf("pay", "");

test("a refused check (jira_auth_failed) names the deployment's token in the token mode and the user's connection in the oauth mode", () => {
  const refused = { result: "failed", reason: "jira_auth_failed", field: null } as const;
  const alertOf = (atlassian: typeof TOKEN | typeof OAUTH) => {
    const st = apply(INITIAL_TAB, { type: "loaded", settings: NONE, atlassian }, { type: "start", kind: "check", run: 1 });
    const outcome = apply(st, { type: "checked", run: 1, access: refused }).outcome;
    return outcome?.kind === "problem" ? outcome.texts.alert : undefined;
  };
  assert.equal(alertOf(TOKEN), "Jira refused the Specflow account. An administrator checks the Atlassian token.");
  assert.equal(alertOf(OAUTH), "Atlassian refused the connection. Reconnect Atlassian in Settings → Jira.");
});

test("flow: check -> save -> switch; the typed keys seed once, each answer is applied and busy ends clear", () => {
  let st = loaded();
  assert.equal(phase(st), "check");
  st = apply(st, { type: "edit", field: "project", value: "pay", run: 1 }, { type: "start", kind: "check", run: 2 });
  assert.equal(phase(st), "checking");
  st = apply(st, { type: "checked", run: 2, access: OK });
  assert.equal(st.busy, null);
  assert.equal(phase(st), "save");
  assert.equal(st.outcome?.kind, "ok");

  st = apply(st, { type: "start", kind: "save", run: 3 });
  assert.equal(phase(st), "saving");
  st = apply(st, { type: "saved", run: 3, settings: SAVED, target });
  assert.equal(st.busy, null);
  assert.equal(st.outcome, null);
  assert.equal(st.project, "PAY"); // the server's spelling
  assert.deepEqual(st.settings, SAVED);
  assert.equal(phase(st), "check");
  assert.ok(isSavedTarget(SAVED, st.project, st.epic));

  st = apply(st, { type: "start", kind: "switch", run: 4 }, { type: "switched", settings: { ...SAVED, enabled: true } });
  assert.equal(st.busy, null);
  assert.equal(typeof st.settings === "object" && st.settings?.enabled, true);
  // a later load keeps what the owner typed: the form is seeded only once
  st = apply(st, { type: "edit", field: "project", value: "XYZ", run: 5 }, { type: "loaded", settings: SAVED, atlassian: TOKEN });
  assert.equal(st.project, "XYZ");
});

test("stale guard: an edit after Check access drops the answer, and an older check never clears a newer check's busy", () => {
  let st = apply(loaded(), { type: "start", kind: "check", run: 1 }, { type: "edit", field: "project", value: "PA", run: 2 });
  assert.equal(st.busy, null);
  st = apply(st, { type: "checked", run: 1, access: OK });
  assert.equal(st.outcome, null);
  assert.equal(st.busy, null);

  st = apply(st, { type: "start", kind: "check", run: 3 }, { type: "checked", run: 1, access: OK }); // an old answer arrives late
  assert.equal(st.outcome, null);
  assert.equal(st.busy, "check");
  st = apply(st, { type: "checked", run: 3, access: OK });
  assert.equal(st.outcome?.kind, "ok");
  assert.equal(st.busy, null);

  const failed = apply(
    loaded(),
    { type: "edit", field: "project", value: "P", run: 1 },
    {
      type: "failed",
      kind: "check",
      run: 0,
      failure: { kind: "fields", texts: { project: "x" } },
    },
  );
  assert.equal(failed.outcome, null); // a stale failure writes no text under the field
});

test("U1: editing while a Save is in flight does not wedge the button, and the saved answer is still applied", () => {
  let st = apply(loaded(), { type: "edit", field: "project", value: "pay", run: 1 }, { type: "start", kind: "save", run: 2 });
  st = apply(st, { type: "edit", field: "epic", value: "PAY-1", run: 3 });
  assert.equal(st.busy, "save"); // still saving: the button reads Saving...
  st = apply(st, { type: "saved", run: 2, settings: SAVED, target });
  assert.equal(st.busy, null); // the old code left "save" here for good
  assert.deepEqual(st.settings, SAVED); // and dropped the answer, so the UI said "no target"
  assert.equal(st.epic, "PAY-1"); // the newer typing is kept
  assert.equal(st.outcome, null);
  assert.equal(phase(st), "check");
});

test("U1, U4: a Switch answer applies even after an edit or a primary click; the switch never stays busy", () => {
  let st = apply(loaded(SAVED), { type: "start", kind: "switch", run: 1 }, { type: "edit", field: "project", value: "PA", run: 2 });
  assert.equal(st.busy, "switch");
  st = apply(st, { type: "switched", settings: { ...SAVED, enabled: true } });
  assert.equal(st.busy, null);
  assert.equal(typeof st.settings === "object" && st.settings?.enabled, true);

  // a check started while the switch runs: the switch answer does not end the check, the check answer does
  st = apply(loaded(SAVED), { type: "start", kind: "switch", run: 1 }, { type: "start", kind: "check", run: 2 });
  st = apply(st, { type: "switched", settings: { ...SAVED, enabled: true } });
  assert.equal(st.busy, "check");
  st = apply(st, { type: "checked", run: 2, access: OK });
  assert.equal(st.busy, null);
});

test("busy always clears: every call kind, answered fresh or stale, by an answer or a failure", () => {
  const kinds: Busy[] = ["check", "save", "switch"];
  const failures = [
    { kind: "toast", text: "t" },
    { kind: "fields", texts: { project: "p" } },
    { kind: "unavailable" },
    { kind: "gone" },
  ] as const;
  for (const kind of kinds) {
    for (const stale of [false, true]) {
      const answers: Record<Busy, TabAction> = {
        check: { type: "checked", run: 1, access: OK },
        save: { type: "saved", run: 1, settings: SAVED, target },
        switch: { type: "switched", settings: SAVED },
      };
      const mid: TabAction[] = stale ? [{ type: "edit", field: "epic", value: "PAY-1", run: 2 }] : [];
      const ends = [answers[kind], ...failures.map((failure): TabAction => ({ type: "failed", kind, run: 1, failure }))];
      for (const end of ends) {
        const st = apply(loaded(), { type: "start", kind, run: 1 }, ...mid, end);
        assert.equal(st.busy, null, `${kind} ${stale ? "stale" : "fresh"} ${end.type}`);
      }
    }
  }
});

test("failures: unavailable turns the form into the notice from the latest settings; load_failed keeps loaded settings", () => {
  const live = loaded(SAVED);
  const st = apply(live, { type: "failed", kind: "save", run: 0, failure: { kind: "unavailable" } });
  assert.deepEqual(st.settings, { ...SAVED, available: false });
  assert.deepEqual(apply(live, { type: "load_failed" }).settings, SAVED);
  assert.equal(apply(INITIAL_TAB, { type: "load_failed" }).settings, "failed");
  assert.equal(apply(INITIAL_TAB, { type: "load_failed" }, { type: "retry" }).settings, null);
  const err = new DataError("network", false, { apiCode: "jira_unavailable" });
  assert.equal(failureOf(err).kind, "unavailable");
});

test("polling: reloads every 5 s while the status is running, stops when it is not, and the stop function clears the timer", (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const load = mock.fn();
  const running: JiraSettings = { ...SAVED, sync: { status: "running", finishedAt: null, errorCode: null, progress: null } };
  assert.equal(isRunning(running), true);
  assert.equal(isRunning({ ...running, sync: { status: "succeeded", finishedAt: null, errorCode: null, progress: null } }), false);
  assert.equal(isRunning(NONE) || isRunning("failed") || isRunning(null), false);

  assert.equal(startPolling(false, load), undefined); // not running: no timer
  t.mock.timers.tick(POLL_MS * 3);
  assert.equal(load.mock.callCount(), 0);

  const stop = startPolling(isRunning(running), load);
  assert.ok(stop);
  t.mock.timers.tick(POLL_MS - 1);
  assert.equal(load.mock.callCount(), 0);
  t.mock.timers.tick(1);
  assert.equal(load.mock.callCount(), 1);
  t.mock.timers.tick(POLL_MS * 2);
  assert.equal(load.mock.callCount(), 3);
  stop();
  t.mock.timers.tick(POLL_MS * 5);
  assert.equal(load.mock.callCount(), 3); // the status turned succeeded (or the tab unmounted): no more loads
});
