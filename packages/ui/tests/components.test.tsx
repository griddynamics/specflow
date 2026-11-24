import { test } from "node:test";
import assert from "node:assert/strict";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { siGithub, siJira } from "simple-icons";
import {
  Brand,
  BrandMark,
  Button,
  ButtonLink,
  Card,
  Dialog,
  Field,
  GLYPH_PATHS,
  GraphicoRoot,
  LOGO_PATHS,
  Select,
  TextArea,
  TextInput,
  VendorMark,
} from "../src/index";
import type { VendorKind } from "../src/index";

function html(node: ReactElement): string {
  return renderToStaticMarkup(node);
}

test("Button: ink by default, never submits, variants and size add classes", () => {
  assert.equal(html(<Button>Go</Button>), '<button type="button" class="gx-btn">Go</button>');
  assert.match(html(<Button variant="ghost">x</Button>), /class="gx-btn gx-btn--ghost"/);
  assert.match(
    html(
      <Button variant="yellow" size="sm">
        x
      </Button>,
    ),
    /class="gx-btn gx-btn--yellow gx-btn--sm"/,
  );
  assert.match(
    html(
      <Button type="submit" disabled>
        x
      </Button>,
    ),
    /disabled="" type="submit"/,
  );
});

test("ButtonLink renders an anchor with the same look", () => {
  assert.equal(
    html(
      <ButtonLink href="/a" variant="ghost">
        Open
      </ButtonLink>,
    ),
    '<a href="/a" class="gx-btn gx-btn--ghost">Open</a>',
  );
});

test("Dialog: a native dialog named by its title, with a labelled close button", () => {
  const view = html(
    <Dialog open={false} onClose={() => undefined} title="Members" eyebrow="Workspace" closeLabel="Close members">
      <p>body</p>
    </Dialog>,
  );
  assert.match(view, /^<dialog class="gx-dialog" aria-labelledby="([^"]+)"/);
  const id = /aria-labelledby="([^"]+)"/.exec(view)?.[1] ?? "";
  assert.match(view, new RegExp(`<h2 id="${id}" class="gx-dialog__title">Members</h2>`));
  assert.match(view, /<p class="gx-eyebrow gx-dialog__eyebrow">Workspace<\/p>/);
  assert.match(view, /<button type="button" class="gx-dialog__close" aria-label="Close members">/);
  assert.match(view, /<p>body<\/p>/);
});

test("Dialog without an eyebrow renders none", () => {
  assert.doesNotMatch(html(<Dialog open={false} onClose={() => undefined} title="T" />), /gx-eyebrow/);
});

test("Field: label points at the control; hint and error only when given; the error is an alert", () => {
  const view = html(
    <Field label="Name" htmlFor="n">
      <TextInput id="n" />
    </Field>,
  );
  assert.equal(view, '<div class="gx-field"><label class="gx-field__label" for="n">Name</label><input id="n" class="gx-input"/></div>');
  const utils = html(
    <Field label="Note" htmlFor="t" hint="Short" error="Required">
      <TextArea id="t" />
    </Field>,
  );
  assert.match(utils, /<textarea id="t" class="gx-input"><\/textarea>/);
  assert.match(utils, /<p class="gx-field__hint">Short<\/p>/);
  assert.match(utils, /<p class="gx-field__error" role="alert">Required<\/p>/);
});

test("Select and TextInput keep the caller's class beside the shared one", () => {
  assert.match(html(<Select className="wide" />), /<select class="gx-input wide"><\/select>/);
  assert.match(html(<TextInput className="wide" />), /class="gx-input wide"/);
});

test("Card renders a div by default and the requested element otherwise", () => {
  assert.equal(html(<Card>x</Card>), '<div class="gx-card">x</div>');
  assert.equal(
    html(
      <Card as="li" className="c">
        x
      </Card>,
    ),
    '<li class="gx-card c">x</li>',
  );
});

test("Brand: mark plus word; a link with href; compact hides the word visually only", () => {
  assert.match(html(<BrandMark />), /^<svg class="gx-brand__mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">/);
  assert.match(html(<Brand />), /^<span class="gx-brand"><svg.*<\/svg><span>Graphico<\/span><\/span>$/);
  assert.match(html(<Brand href="/" />), /^<a class="gx-brand" href="\/">/);
  assert.match(html(<Brand compact />), /<span class="gx-vh">Graphico<\/span>/);
});

test("VendorMark: simple-icons paths unchanged, signs drawn, decorative unless titled", () => {
  assert.equal(LOGO_PATHS.github, siGithub.path);
  assert.equal(LOGO_PATHS.jira, siJira.path);
  assert.match(
    html(<VendorMark kind="github" />),
    new RegExp(`aria-hidden="true".*d="${siGithub.path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`),
  );
  assert.match(html(<VendorMark kind="github" title="GitHub" />), /role="img" aria-label="GitHub"/);
  assert.doesNotMatch(html(<VendorMark kind="github" title="GitHub" />), /aria-hidden/);
  assert.match(html(<VendorMark kind="slack" />), /class="gx-mark gx-mark--glyph".*d="M9\.5 3\.5 7\.5 20\.5/);
  const kinds: VendorKind[] = ["github", "jira", "confluence", "gdrive", "mcp", "slack", "upload"];
  for (const kind of kinds) assert.match(html(<VendorMark kind={kind} />), /<path d="[^"]+"><\/path>/, kind);
  assert.deepEqual(Object.keys(GLYPH_PATHS), ["slack", "upload"]);
});

test("GraphicoRoot: the gx class, the theme attribute, the font links; fonts can be turned off", () => {
  const view = html(
    <GraphicoRoot theme="dark" className="page">
      <p>x</p>
    </GraphicoRoot>,
  );
  assert.match(view, /class="gx page"/);
  assert.match(view, /data-theme="dark"/);
  assert.match(view, /href="https:\/\/fonts\.googleapis\.com\/css2\?family=Fira\+Code/);
  assert.match(view, /href="https:\/\/fonts\.gstatic\.com"/);
  const utils = html(<GraphicoRoot fonts={false}>x</GraphicoRoot>);
  assert.equal(utils, '<div class="gx">x</div>');
});
