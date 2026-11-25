// The menu's blur rule: a press inside the list is not "the focus left", whatever `relatedTarget` says (it is null in browsers that do not focus
// a button on click, just before the click lands); a blur with no press and no target inside is.
import { test } from "node:test";
import assert from "node:assert/strict";
import { focusLeftMenu } from "../src/components/menu-focus";

test("a blur with no related target during a press does not close the menu (Safari, Firefox on macOS)", () => {
  assert.equal(focusLeftMenu(true, false), false);
});

test("a blur to something outside the menu, with no press, closes it (Tab out, a click elsewhere)", () => {
  assert.equal(focusLeftMenu(false, false), true);
});

test("a blur to something inside the menu never closes it, pressed or not", () => {
  assert.equal(focusLeftMenu(false, true), false);
  assert.equal(focusLeftMenu(true, true), false);
});
