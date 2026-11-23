// docs/jira-sync/ui.md#jira-tab, #errors-shown (FR-JIRA-UI-01..05, render side): the Jira tab rendered on the server for the owner and a
// collaborator in each state, and its rules (button phase, where a failure shows, texts, status words) run without React. The click flow
// (check, save, switch, polling) is proven in the browser e2e.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import { DataError } from "@/lib/domain/errors";
import type { JiraSettings, JiraSyncProgress, JiraSyncState } from "@/lib/domain/jira";
import type { JiraViewProps } from "../../components/settings/jira-parts";

stubCss();
async function load() {
  return {
    P: await import("../../components/settings/jira-parts"),
    L: await import("../../components/settings/jira-logic"),
  };
}
let P: Awaited<ReturnType<typeof load>>["P"];
let L: Awaited<ReturnType<typeof load>>["L"];
before(async () => {
  ({ P, L } = await load());
});

const noop = () => undefined;
const SETTINGS: JiraSettings = {
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
const saved = (over: Partial<JiraSettings> = {}): JiraSettings => ({
  ...SETTINGS,
  jiraProject: "PAY",
  enabled: true,
  sync: { status: "succeeded", finishedAt: "2026-02-01T10:00:00Z", errorCode: null, progress: null },
  ...over,
});
function markup(over: Partial<JiraViewProps> = {}): string {
  return renderToStaticMarkup(
    createElement(P.JiraView, {
      settings: SETTINGS,
      projectName: "Checkout revamp",
      project: "",
      epic: "",
      phase: "check",
      outcome: null,
      switching: false,
      onProject: noop,
      onEpic: noop,
      onPrimary: noop,
      onSwitch: noop,
      ...over,
    }),
  );
}
const count = (text: string, re: RegExp) => [...text.matchAll(re)].length;
const OK = { kind: "ok", projectName: "Payments platform", epicSummary: "Checkout revamp" } as const;

test("UI-01: the owner's empty tab has both fields, one primary Check access button disabled until a key is typed, the switch off and disabled", () => {
  const view = markup();
  assert.match(view, /Jira space key/);
  assert.match(view, /placeholder="ABC"/);
  assert.match(view, /Epic key/);
  assert.match(view, /placeholder="ABC-123"/);
  assert.match(view, /Leave empty and Specflow creates an Epic named after the project\./);
  assert.equal(count(view, /data-testid="jira-primary"/g), 1);
  assert.match(view, /<button[^>]*disabled=""[^>]*data-testid="jira-primary"[^>]*>Check access</);
  assert.match(view, /role="switch"[^>]*/);
  assert.match(view, /<button[^>]*role="switch"[^>]*disabled=""/);
  assert.match(view, /aria-checked="false"/);
  assert.match(view, />Sync to Jira</);
  assert.match(view, /After every plan run, Specflow updates the Epic, its Stories and Sub-tasks\./);
  assert.doesNotMatch(view, /jira-unavailable|jira-created-epic|jira-check/);
  assert.doesNotMatch(view, /<h1/);
  assert.doesNotMatch(view, /gdg-seg/);
  assert.match(view, /<section[^>]*role="status"[^>]*data-testid="jira-status"><\/section>/); // U5: the live region is always mounted
});

test("U3: the primary button is disabled while the typed keys are the saved target, and enabled again on the first edit", () => {
  const view = markup({ settings: saved(), project: "pay", epic: "" });
  assert.match(view, /<button[^>]*disabled=""[^>]*data-testid="jira-primary"/);
  assert.doesNotMatch(markup({ settings: saved(), project: "PAYX" }), /<button[^>]*disabled=""[^>]*data-testid="jira-primary"/);
  assert.doesNotMatch(
    markup({ settings: saved(), project: "PAY", epic: "PAY-2" }),
    /<button[^>]*disabled=""[^>]*data-testid="jira-primary"/,
  );
  assert.doesNotMatch(
    markup({ settings: saved(), project: "PAY", outcome: { kind: "problem", texts: { alert: "x" } } }),
    /<button[^>]*disabled=""[^>]*data-testid="jira-primary"/,
  );
});

test("U4, U5, U6: a busy switch keeps focus (aria-busy, not disabled); a check result is a status; a bad time shows as sent", () => {
  const view = markup({ settings: saved(), project: "PAY", switching: true });
  assert.match(view, /<button[^>]*role="switch"[^>]*aria-busy="true"/);
  assert.doesNotMatch(view, /<button[^>]*role="switch"[^>]*disabled/);
  assert.match(markup({ project: "PAY", phase: "save", outcome: OK }), /<p[^>]*role="status"[^>]*data-testid="jira-check"/);
  assert.match(
    markup({ settings: saved({ sync: { status: "failed", finishedAt: "garbage", errorCode: null, progress: null } }), project: "PAY" }),
    />garbage</,
  );
});

test("UI-01: the button reads Check access, Checking… (disabled), Save, Saving… by phase", () => {
  assert.match(markup({ project: "PAY", phase: "check" }), />Check access</);
  assert.doesNotMatch(markup({ project: "PAY", phase: "check" }), /<button[^>]*disabled=""[^>]*data-testid="jira-primary"/);
  assert.match(markup({ project: "PAY", phase: "checking" }), /<button[^>]*disabled=""[^>]*data-testid="jira-primary"[^>]*>Checking…</);
  assert.match(markup({ project: "PAY", phase: "save", outcome: OK }), />Save</);
  assert.match(markup({ project: "PAY", phase: "saving" }), /<button[^>]*disabled=""[^>]*data-testid="jira-primary"[^>]*>Saving</);
  assert.equal(L.phaseOf(null, null), "check");
  assert.equal(L.phaseOf(null, "check"), "checking");
  assert.equal(L.phaseOf(OK, "save"), "saving");
  assert.equal(L.phaseOf(OK, null), "save");
  assert.equal(L.phaseOf({ kind: "problem", texts: { alert: "x" } }, null), "check");
});

test("UI-02: an ok check shows the Jira space and the Epic, or that Specflow creates one named after the project", () => {
  let view = markup({ project: "PAY", epic: "pay-12", phase: "save", outcome: OK });
  assert.match(view, /data-testid="jira-check"/);
  assert.match(view, /Jira space Payments platform/);
  assert.match(view, /Epic PAY-12: Checkout revamp/);
  view = markup({ project: "PAY", phase: "save", outcome: { ...OK, epicSummary: null } });
  assert.match(view, /Specflow creates an Epic named Checkout revamp/);
  assert.match(markup({ project: "PAY", phase: "save", outcome: OK, projectName: null }), /Specflow creates an Epic named the project\./);
});

test("UI-02: a failed check puts its text under the field it names, or in an alert line; a failure never enables Save", () => {
  const cases: [Parameters<typeof L.outcomeOf>[0], RegExp, boolean][] = [
    [
      { result: "failed", reason: "jira_not_found", field: "jira_project" },
      /No Jira space with this key, or the account cannot see it\./,
      false,
    ],
    [
      { result: "failed", reason: "jira_issue_types_missing", field: "jira_project" },
      /This Jira space has no Epic, Story or Sub-task issue type\./,
      false,
    ],
    [
      { result: "failed", reason: "jira_forbidden", field: "jira_project" },
      /The Specflow account may not create, edit, link or rank issues in this space\./,
      false,
    ],
    [{ result: "failed", reason: "jira_not_found", field: "epic_key" }, /No issue with this key\./, false],
    [{ result: "failed", reason: "jira_epic_invalid", field: "epic_key" }, /This issue is not an Epic of the Jira space\./, false],
    [
      { result: "failed", reason: "jira_auth_failed", field: null },
      /Jira refused the Specflow account\. An administrator checks the Atlassian token\./,
      true,
    ],
    [{ result: "failed", reason: "jira_unreachable", field: null }, /Jira did not answer\. Try again\./, true],
    [{ result: "failed", reason: "jira_unexpected", field: null }, /Jira did not answer\. Try again\./, true],
  ];
  for (const [access, text, alert] of cases) {
    const outcome = L.outcomeOf(access);
    const view = markup({ project: "PAY", epic: "PAY-1", outcome, phase: L.phaseOf(outcome, null) });
    assert.match(view, text, access.result === "failed" ? access.reason : "");
    assert.equal(view.includes('data-testid="jira-alert"'), alert);
    assert.equal(view.includes("gdg-field--error"), !alert);
    assert.match(view, />Check access</);
  }
  const view = markup({
    project: "PAY",
    epic: "PAY-1",
    outcome: L.outcomeOf({ result: "failed", reason: "jira_epic_invalid", field: "epic_key" }),
  });
  assert.equal(count(view, /gdg-field--error/g), 1);
  assert.match(view, /<span class="gdg-field__help" id="[^"]*" role="alert">This issue is not an Epic/);
});

test("UI-03: a saved target enables the switch, which shows its state; the fields keep the saved keys", () => {
  let view = markup({ settings: saved(), project: "PAY", epic: "" });
  assert.match(view, /aria-checked="true"/);
  assert.doesNotMatch(view, /<button[^>]*role="switch"[^>]*disabled/);
  assert.match(view, /value="PAY"/);
  view = markup({ settings: saved({ enabled: false }), project: "PAY" });
  assert.match(view, /aria-checked="false"/);
  assert.doesNotMatch(view, /<button[^>]*role="switch"[^>]*disabled/);
});

test("UI-03: the Epic Specflow created is a link to the site while no Epic key is set, and is hidden when one is", () => {
  const view = markup({ settings: saved({ createdEpicKey: "PAY-9" }), project: "PAY" });
  assert.match(view, /Epic created by Specflow:/);
  assert.match(view, /<a [^>]*href="https:\/\/example\.atlassian\.net\/browse\/PAY-9"[^>]*>PAY-9</);
  assert.match(view, /rel="noopener noreferrer"/);
  assert.doesNotMatch(
    markup({ settings: saved({ createdEpicKey: "PAY-9", epicKey: "PAY-1" }), project: "PAY", epic: "PAY-1" }),
    /jira-created-epic/,
  );
});

test("UI-04: each status is a word in a tag, with the finish time and the error text when there is one", () => {
  const words: [JiraSyncState, string][] = [
    ["idle", "Never synced"],
    ["running", "Syncing"],
    ["succeeded", "Synced"],
    ["partial", "Synced with errors"],
    ["failed", "Failed"],
    ["skipped", "Skipped"],
  ];
  for (const [status, word] of words) {
    assert.equal(L.statusWord(status), word);
    const view = markup({ settings: saved({ sync: { status, finishedAt: null, errorCode: null, progress: null } }), project: "PAY" });
    assert.match(
      view,
      new RegExp(`data-testid="jira-status"[^>]*><span class="gdg-tag[^"]*"[^>]*>(<span[^>]*></span>)?${word}</span>`),
      status,
    );
  }
  const view = markup({
    settings: saved({ sync: { status: "failed", finishedAt: "2026-02-01T10:00:00Z", errorCode: "jira_auth_failed", progress: null } }),
    project: "PAY",
  });
  assert.match(view, /2026/);
  assert.match(view, /Jira refused the Specflow account/);
  assert.match(view, /gdg-tag--danger/);
  assert.match(
    markup({ settings: saved({ sync: { status: "running", finishedAt: null, errorCode: null, progress: null } }), project: "PAY" }),
    /gdg-tag--pending/,
  );
});

test("UI-04: every sync error code has its text; an unknown code says something went wrong", () => {
  const texts: [string, string][] = [
    ["jira_auth_failed", "Jira refused the Specflow account. An administrator checks the Atlassian token."],
    ["jira_forbidden", "The Specflow account may not create, edit, link or rank issues in this space."],
    ["jira_not_found", "No Jira space with this key, or the account cannot see it."],
    ["jira_unreachable", "Jira did not answer. Try again."],
    ["jira_unexpected", "Jira did not answer. Try again."],
    ["no_plan", "No plan yet."],
    ["plan_applying", "The plan was being updated."],
    ["jira_not_configured", "Sync is off."],
    ["interrupted", "Stopped by a restart."],
    ["target_changed", "The Jira target changed during the sync."],
    ["unexpected_error", "Something went wrong."],
    ["constructor", "Something went wrong."],
  ];
  for (const [code, text] of texts) assert.equal(L.reasonText(code), text, code);
  assert.equal(L.reasonText("jira_not_found", "epic_key"), "No issue with this key.");
});

test("UI-05: a collaborator sees the saved values and the status with every control disabled and no primary button", () => {
  const view = markup({ settings: saved({ editable: false }), project: "PAY" });
  assert.doesNotMatch(view, /jira-primary/);
  assert.equal(count(view, /<input[^>]*disabled=""/g), 2);
  assert.match(view, /<button[^>]*role="switch"[^>]*disabled=""/);
  assert.match(view, /value="PAY"/);
  assert.match(view, /data-testid="jira-status"/);
});

test("UI-05: when Jira is not connected the notice shows and everything is disabled, for the owner too", () => {
  const view = markup({ settings: saved({ available: false, siteUrl: null }), project: "PAY" });
  assert.match(view, /data-testid="jira-unavailable"/);
  assert.match(view, /Jira is not connected to this Specflow server\. An administrator connects an Atlassian account\./);
  assert.equal(count(view, /<input[^>]*disabled=""/g), 2);
  assert.match(view, /<button[^>]*disabled=""[^>]*data-testid="jira-primary"/);
  assert.match(view, /<button[^>]*role="switch"[^>]*disabled=""/);
  assert.doesNotMatch(markup(), /jira-unavailable/);
});

test("the typed keys go to the API trimmed and upper-cased; a blank Epic is none", () => {
  assert.deepEqual(L.targetOf(" pay ", " "), { jiraProject: "PAY", epicKey: null });
  assert.deepEqual(L.targetOf("pay", "pay-12"), { jiraProject: "PAY", epicKey: "PAY-12" });
});

test("failures: validation puts the key text under the field named by loc[1]; owner_required and the rest are a toast; the notice and home have their own kinds", () => {
  const err = (code: DataError["code"], apiCode: string, errors?: { loc: string[]; type: string }[]) =>
    new DataError(code, false, { apiCode, errors });
  assert.deepEqual(L.failureOf(err("validation", "validation_failed", [{ loc: ["body", "jira_project"], type: "x" }])), {
    kind: "fields",
    texts: { project: "Use a Jira key such as ABC or ABC-123." },
  });
  assert.deepEqual(
    L.failureOf(
      err("validation", "validation_failed", [
        { loc: ["body", "jira_project"], type: "x" },
        { loc: ["body", "epic_key"], type: "x" },
      ]),
    ),
    { kind: "fields", texts: { project: "Use a Jira key such as ABC or ABC-123.", epic: "Use a Jira key such as ABC or ABC-123." } },
  );
  assert.deepEqual(L.failureOf(err("validation", "validation_failed", [{ loc: ["body", "enabled"], type: "x" }])), {
    kind: "toast",
    text: "Could not save. Try again.",
  });
  assert.deepEqual(L.failureOf(err("forbidden", "owner_required")), { kind: "toast", text: "Could not save. Try again." });
  assert.deepEqual(L.failureOf(new DataError("network", true)), { kind: "toast", text: "Could not save. Try again." });
  assert.deepEqual(L.failureOf(err("unavailable", "jira_unavailable")), { kind: "unavailable" });
  assert.deepEqual(L.failureOf(err("not_found", "project_not_found")), { kind: "gone" });
});

// ---- Progress of a running pass (ui.md#progress) ----

const running = (progress: JiraSyncProgress | null): JiraSettings =>
  saved({ sync: { status: "running", finishedAt: null, errorCode: null, progress } });

test("progress: StatusLine shows the step, 'done of total' and a progressbar while running; without progress only the word", () => {
  const steps: [JiraSyncProgress["step"], string][] = [
    ["issues", "Creating and updating issues"],
    ["links", "Adding links"],
    ["order", "Ordering issues"],
  ];
  for (const [step, text] of steps) {
    const view = markup({ settings: running({ step, done: 23, total: 60 }), project: "PAY" });
    assert.match(view, new RegExp(`data-testid="jira-progress"[^>]*data-step="${step}"`));
    assert.ok(view.includes(`<span>${text}</span><span>23 of 60</span>`), step);
    assert.match(
      view,
      /role="progressbar"[^>]*aria-valuemin="0"[^>]*aria-valuemax="60"[^>]*aria-valuenow="23"[^>]*aria-valuetext="23 of 60"/,
    );
    assert.match(view, /gdg-progress__fill" style="width:38\.33/);
  }
  assert.match(markup({ settings: running(null), project: "PAY" }), /gdg-tag--pending/);
  assert.doesNotMatch(markup({ settings: running(null), project: "PAY" }), /jira-progress|progressbar/);
  // a finished pass never shows a bar, whatever the status carries
  const finished = saved({
    sync: { status: "succeeded", finishedAt: null, errorCode: null, progress: { step: "issues", done: 1, total: 2 } },
  });
  assert.doesNotMatch(markup({ settings: finished, project: "PAY" }), /progressbar/);
});

test("progress: done is clamped to total, a total of 0 shows the step text alone", () => {
  assert.deepEqual(L.progressView({ step: "issues", done: 9, total: 5 }), {
    step: "Creating and updating issues",
    count: "5 of 5",
    done: 5,
    total: 5,
  });
  const none = running({ step: "links", done: 0, total: 0 });
  assert.ok(markup({ settings: none, project: "PAY" }).includes("<span>Adding links</span></div>"));
  assert.doesNotMatch(markup({ settings: none, project: "PAY" }), /progressbar|0 of 0/);
  assert.ok(line(saved({ sync: syncOf("running", { step: "order", done: 0, total: 0 }) })).includes("<span>Ordering issues</span></div>"));
  assert.match(
    markup({ settings: running({ step: "order", done: 9, total: 5 }), project: "PAY" }),
    /aria-valuenow="5"[^>]*aria-valuetext="5 of 5"/,
  );
});

// ---- The Plan's Jira line (ui.md#plan-line) ----

let J: Awaited<ReturnType<typeof loadLine>>;
const loadLine = () => import("../../components/plan/ready/JiraSyncLine");
before(async () => {
  J = await loadLine();
});
const HREF = "/projects/p1/settings?tab=jira";
const line = (settings: JiraSettings): string => renderToStaticMarkup(createElement(J.JiraSyncLineView, { settings, settingsHref: HREF }));
const syncOf = (status: JiraSyncState, progress: JiraSyncProgress | null = null): JiraSettings["sync"] => ({
  status,
  finishedAt: null,
  errorCode: null,
  progress,
});

test("plan line: running with progress shows the step, the count and a bar", () => {
  const view = line(running({ step: "links", done: 4, total: 10 }));
  assert.match(view, /data-testid="plan-jira"[^>]*data-status="running"/);
  assert.match(view, /data-testid="plan-jira-progress"/);
  assert.ok(view.includes("<span>Adding links</span><span>4 of 10</span>"));
  assert.match(view, /role="progressbar"[^>]*aria-valuenow="4"/);
});

test("plan line: running without progress says the sync is queued, with no bar", () => {
  const view = line(running(null));
  assert.ok(view.includes("<span>Jira sync queued</span>"));
  assert.doesNotMatch(view, /progressbar/);
});

test("plan line: succeeded says Jira is up to date and links the Epic (created or chosen) on https sites only", () => {
  const ok = saved({ createdEpicKey: "PAY-9", sync: syncOf("succeeded") });
  const view = line(ok);
  assert.ok(view.includes("Jira is up to date"));
  assert.match(view, /<a [^>]*href="https:\/\/example\.atlassian\.net\/browse\/PAY-9"[^>]*rel="noopener noreferrer"[^>]*>Epic PAY-9<\/a>/);
  assert.match(line({ ...ok, createdEpicKey: null, epicKey: "PAY-3" }), /browse\/PAY-3"[^>]*>Epic PAY-3</);
  assert.doesNotMatch(line({ ...ok, siteUrl: null }), /<a /); // no (https) site: no link
  assert.doesNotMatch(line({ ...ok, createdEpicKey: null, epicKey: null }), /<a /);
});

test("plan line: partial and failed say so and link to the Jira tab of Settings", () => {
  for (const [status, text] of [
    ["partial", "Jira sync finished with errors."],
    ["failed", "Jira sync failed."],
  ] as const) {
    const view = line(saved({ sync: syncOf(status) }));
    assert.ok(view.includes(text), status);
    assert.match(view, /<a [^>]*href="\/projects\/p1\/settings\?tab=jira"[^>]*>Open Jira settings<\/a>/);
  }
});

test("plan line: hidden when Jira is unavailable or off, there is no target or sync, or the sync never ran or was skipped", () => {
  const base = saved({ sync: syncOf("succeeded") });
  assert.notEqual(line(base), "");
  assert.equal(line({ ...base, available: false }), "");
  assert.equal(line({ ...base, enabled: false }), "");
  assert.equal(line({ ...base, jiraProject: null }), "");
  assert.equal(line({ ...base, sync: null }), "");
  assert.equal(line({ ...base, sync: syncOf("idle") }), "");
  assert.equal(line({ ...base, sync: syncOf("skipped") }), "");
});
