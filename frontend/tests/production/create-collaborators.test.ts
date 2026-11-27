// docs/projects/ui.md#create-project-modal (FR-PRJ-UI-09, UI-10): the Create Project form logic (body building, chip errors of a 422),
// the PeoplePicker email-mode rules and the modal rendered on the server with a fake router and fake ports.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import type { AppRouterInstance } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { DataError } from "@/lib/domain/errors";

stubCss();
async function load() {
  return {
    F: await import("@/components/shared/create-project/form-logic"),
    A: await import("@/components/shared/ui/app-logic"),
    PP: await import("../../components/ui/PeoplePicker"),
    M: await import("../../components/create-project/CreateProjectModal"),
    D: await import("@/lib/data/provider"),
    R: await import("next/dist/shared/lib/app-router-context.shared-runtime"),
  };
}
let X: Awaited<ReturnType<typeof load>>;
before(async () => {
  X = await load();
});

const NAME_PATH = "body";
const FIELD = "collaborators";
const ANN = "a@example.com";
const TOO_LONG = "x".repeat(201);
const validation = (errors: { loc: (string | number)[]; type: string }[]) =>
  new DataError("validation", false, { status: 422, apiCode: "validation_failed", errors });

test("FR-PRJ-UI-09: two chips give {name, collaboratorEmails}, none give {name} only", () => {
  assert.deepEqual(X.F.buildInput({ name: " Pilot ", emails: [ANN, "b@example.com"] }), {
    name: "Pilot",
    collaboratorEmails: [ANN, "b@example.com"],
  });
  const none = X.F.buildInput({ name: "Pilot", emails: [] });
  assert.deepEqual(none, { name: "Pilot" });
  assert.equal(JSON.stringify(none), '{"name":"Pilot"}');
  assert.equal(X.F.buildInput({ name: "  ", emails: [ANN] }), null);
  assert.deepEqual(X.F.initialForm(), { name: "", emails: [] });
  assert.equal(X.F.COLLABORATOR_CHIPS_MAX, 49);
});

test("FR-PRJ-UI-10: a 422 marks the chips at loc[2] with the ui.md texts and is no name or alert message", () => {
  const f = X.F.createFailure(
    validation([
      { loc: [NAME_PATH, FIELD, 1], type: "invalid_email" },
      { loc: [NAME_PATH, FIELD, 2], type: "email_domain_not_allowed" },
    ]),
    "Pilot",
  );
  assert.deepEqual(f, {
    nameMsg: null,
    alert: false,
    chipMsgs: { 1: "Enter a valid email address.", 2: "This email domain cannot sign in to SpecFlow." },
  });
});

test("FR-PRJ-UI-10: the real API sends the index as a string (every loc part is a string) and the chip is still marked", () => {
  const f = X.F.createFailure(validation([{ loc: [NAME_PATH, FIELD, "0"], type: "email_domain_not_allowed" }]), "Pilot");
  assert.deepEqual(f, { nameMsg: null, alert: false, chipMsgs: { 0: "This email domain cannot sign in to SpecFlow." } });
  const odd = X.F.createFailure(validation([{ loc: [NAME_PATH, FIELD, "x1"], type: "invalid_email" }]), "Pilot");
  assert.deepEqual(odd.chipMsgs, {});
});

test("create failures: a name 422 stays a name message, anything else the alert", () => {
  const name = X.F.createFailure(validation([{ loc: [NAME_PATH, "name"], type: "string_too_long" }]), TOO_LONG);
  assert.equal(name.nameMsg, "Use 200 characters or fewer.");
  assert.deepEqual(name.chipMsgs, {});
  const other = X.F.createFailure(new DataError("network", true), "Pilot");
  assert.deepEqual(other, { nameMsg: null, alert: true, chipMsgs: {} });
  assert.equal(X.F.createFailure("boom", "Pilot").alert, true);
});

test("typed-email rules: trim and lower-case, format, duplicate, limit, empty", () => {
  const c = X.A.commitTyped;
  assert.deepEqual(c([], "  Ann@Example.COM "), { kind: "add", email: "ann@example.com" });
  assert.deepEqual(c([], "   "), { kind: "empty" });
  assert.deepEqual(c([], "ann"), { kind: "invalid" });
  assert.deepEqual(c([], "ann@x"), { kind: "invalid" });
  assert.deepEqual(c([], "a b@example.com"), { kind: "invalid" });
  assert.deepEqual(c(["ann@example.com"], "ANN@example.com"), { kind: "duplicate" });
  assert.deepEqual(c([ANN, "b@example.com"], "c@example.com", 2), { kind: "full" });
  assert.deepEqual(c([ANN], "c@example.com", 2), { kind: "add", email: "c@example.com" });
  assert.equal(X.F.rejectText("invalid"), "Enter a valid email address.");
  assert.equal(X.F.rejectText("duplicate"), "Already a collaborator.");
  assert.equal(X.F.rejectText("full"), "This project has 50 people. Remove someone first.");
});

const html = (el: ReactElement) => renderToStaticMarkup(el);
const person = (email: string, name: string) => ({ id: email, name, email, kind: "person" as const });

test("picker: a typed chip shows its email", () => {
  const view = html(
    createElement(X.PP.PeoplePicker, { people: [], value: ["new@example.com"], allowTyped: true, watchQuery: () => undefined }),
  );
  assert.match(view, /data-testid="people-chip"/);
  assert.match(view, /new@example\.com/);
});

test("picker: callers without email mode are unchanged (names on chips)", () => {
  const view = html(createElement(X.PP.PeoplePicker, { people: [{ id: "u1", name: "Ann Lee" }], value: ["u1"] }));
  assert.match(view, /Ann Lee/);
});

test("picker: without email mode an unknown id renders no chip, as before", () => {
  const view = html(createElement(X.PP.PeoplePicker, { people: [], value: ["u9"] }));
  assert.doesNotMatch(view, /people-chip/);
});

test("picker: async suggestions are not filtered by the text; picked ones are left out; errors mark the chip", () => {
  const view = html(
    createElement(X.PP.PeoplePicker, {
      people: [person("mark@example.com", "Mark Lee"), person("done@example.com", "Done")],
      value: ["done@example.com", "bad@example.com"],
      defaultOpen: true,
      allowTyped: true,
      watchQuery: () => undefined,
      chipErrors: { "bad@example.com": "Enter a valid email address." },
      error: "Already a collaborator.",
    }),
  );
  assert.match(view, /role="option"/);
  assert.match(view, /Mark Lee/);
  assert.equal(view.match(/role="option"/g)?.length, 1);
  assert.match(view, /data-invalid="true"/);
  assert.match(view, /bad@example\.com: Enter a valid email address\./);
  assert.match(view, /Already a collaborator\./);
});

const router: AppRouterInstance = {
  back: () => undefined,
  forward: () => undefined,
  refresh: () => undefined,
  push: () => undefined,
  replace: () => undefined,
  prefetch: () => undefined,
  bfcacheId: "",
};

/** The modal under the production data provider (nothing is fetched while rendering) and a router that does nothing. */
function renderModal(open: boolean): string {
  const modal = createElement(X.M.CreateProjectModal, { open, onClose: () => undefined });
  return html(createElement(X.R.AppRouterContext.Provider, { value: router }, createElement(X.D.DataProvider, null, modal)));
}

test("modal: a Collaborators field by email, no Contributors, closed renders nothing", () => {
  const view = renderModal(true);
  assert.match(view, /Collaborators/);
  assert.match(view, /placeholder="Email address"/);
  assert.doesNotMatch(view, /Contributors/);
  assert.match(view, /data-testid="cp-create"/);
  assert.doesNotMatch(view, /people-chip/);
  assert.equal(renderModal(false), "");
});
