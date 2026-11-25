import { test } from "node:test";
import assert from "node:assert/strict";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  AppFrame,
  AppHeader,
  Avatar,
  BuiltInList,
  CodeLine,
  ConfirmPanel,
  CountTable,
  DialogFoot,
  FileList,
  IntegrationCard,
  IntegrationGrid,
  Menu,
  NavButton,
  NavLink,
  Notice,
  Page,
  PersonList,
  PersonRow,
  PickList,
  PickRow,
  Row,
  SectionHead,
  Split,
  Stack,
  SuggestionList,
  Tag,
  ThemeToggle,
  ToolGrid,
} from "../src/index";

const html = (node: ReactElement): string => renderToStaticMarkup(node);
const noop = (): undefined => undefined;

test("AppHeader: brand, an optional project, then a labelled navigation", () => {
  const view = html(
    <AppHeader brand={<b>Brand</b>} project={<i>Project</i>} navLabel="App">
      <NavLink href="/a" current>
        Home
      </NavLink>
    </AppHeader>,
  );
  assert.match(
    view,
    /^<header class="gx-header"><div class="gx-header__in"><b>Brand<\/b><span class="gx-header__project"><i>Project<\/i><\/span>/,
  );
  assert.match(view, /<nav class="gx-nav" aria-label="App"><a class="gx-nav__item" href="\/a" aria-current="page">Home<\/a><\/nav>/);
  assert.doesNotMatch(html(<AppHeader brand="x" navLabel="App" />), /gx-header__project/);
});

test("NavLink and NavButton: current page, and the dialog hint", () => {
  assert.doesNotMatch(html(<NavLink href="/a">A</NavLink>), /aria-current/);
  assert.match(
    html(
      <NavButton onClick={noop} opensDialog>
        Members
      </NavButton>,
    ),
    /<button type="button" class="gx-nav__item" aria-haspopup="dialog">Members<\/button>/,
  );
  assert.doesNotMatch(html(<NavButton onClick={noop}>x</NavButton>), /aria-haspopup/);
});

test("ThemeToggle: it is named for what it does, and shows the other theme's icon", () => {
  const props = { onToggle: noop, toDarkLabel: "Switch to dark mode", toLightLabel: "Switch to light mode" } as const;
  assert.match(html(<ThemeToggle theme="light" {...props} />), /aria-label="Switch to dark mode"/);
  assert.match(html(<ThemeToggle theme="dark" {...props} />), /aria-label="Switch to light mode"/);
  assert.match(html(<ThemeToggle theme="light" {...props} />), /<path d="M16 12\.6/);
  assert.match(html(<ThemeToggle theme="dark" {...props} />), /<circle cx="10" cy="10" r="3\.4"/);
});

test("Menu: closed, it is one named button that announces a menu", () => {
  const view = html(<Menu trigger="Northwind" label="Projects" items={[{ key: "a", label: "A", onSelect: noop }]} />);
  assert.match(
    view,
    /class="gx-menu__trigger gx-menu__trigger--project" aria-label="Projects: Northwind" aria-haspopup="menu" aria-expanded="false"/,
  );
  assert.match(view, /<svg/);
  assert.doesNotMatch(view, /role="menu"/);
  const utils = html(<Menu look="avatar" trigger="AK" label="Account" items={[]} />);
  assert.match(utils, /gx-menu__trigger--avatar/);
  assert.doesNotMatch(utils, /<svg/);
  assert.match(utils, /aria-label="Account"/, "an element trigger (the avatar) keeps the menu's name");
  assert.match(
    html(<Menu trigger="Projects" label="Projects" items={[]} />),
    /aria-label="Projects" aria-haspopup/,
    "no 'Projects: Projects'",
  );
});

test("Avatar takes initials from a name or an address; an invited person gets the at sign", () => {
  assert.match(html(<Avatar name="Alex Kim" />), />AK<\/span>/);
  assert.match(html(<Avatar name="sam.ortiz@example.test" />), />SO<\/span>/);
  assert.match(html(<Avatar name="x@y.test" invited />), /gx-avatar gx-avatar--invited" aria-hidden="true">@<\/span>/);
});

test("Tag: plain or solid", () => {
  assert.equal(html(<Tag>Owner</Tag>), '<span class="gx-tag">Owner</span>');
  assert.equal(html(<Tag solid>Active</Tag>), '<span class="gx-tag gx-tag--solid">Active</span>');
});

test("PersonRow: name, detail, you, tag and a labelled action; each only when given", () => {
  const view = html(
    <PersonList label="Collaborators">
      <PersonRow
        name="Jordan Lee"
        detail="jordan@example.test"
        youLabel="you"
        tag="Owner"
        action={{ label: "Remove Jordan Lee", onClick: noop }}
      />
    </PersonList>,
  );
  assert.match(view, /<ul class="gx-people" aria-label="Collaborators">/);
  assert.match(view, /<b>Jordan Lee \(you\)<\/b><small>jordan@example\.test<\/small>/);
  assert.match(view, /<span class="gx-tag">Owner<\/span>/);
  assert.match(view, /aria-label="Remove Jordan Lee" title="Remove Jordan Lee"/);
  const utils = html(<PersonRow name="Sam" />);
  assert.doesNotMatch(utils, /<small>|gx-tag|gx-person__x/);
});

test("SuggestionList: one option button per person", () => {
  const view = html(
    <SuggestionList label="Suggested people" items={[{ key: "a", name: "Priya Shah", email: "priya@example.test", onPick: noop }]} />,
  );
  assert.match(view, /<ul class="gx-suggest" aria-label="Suggested people">/);
  assert.match(view, /<li><button type="button" class="gx-suggest__item">/);
  assert.match(view, /<b>Priya Shah<\/b><small>priya@example\.test<\/small>/);
});

test("ConfirmPanel: an alert dialog named by its title with a Keep and an action", () => {
  const view = html(
    <ConfirmPanel title="Remove?" body="Sam loses access." confirmLabel="Remove" keepLabel="Keep" onConfirm={noop} onKeep={noop} />,
  );
  assert.match(view, /role="alertdialog" aria-label="Remove\?"/);
  assert.match(view, /<p>Sam loses access\.<\/p>/);
  assert.match(view, /<button autofocus="" type="button" class="gx-btn gx-btn--ghost gx-btn--sm">Keep/);
  assert.doesNotMatch(view, /disabled/);
  assert.match(
    html(<ConfirmPanel title="t" body="b" confirmLabel="Go" keepLabel="k" busy onConfirm={noop} onKeep={noop} />),
    /disabled="" type="button" class="gx-btn gx-btn--sm">Go/,
  );
});

test("IntegrationCard: each state sets data-state; the tag shows the API's word; the button is named", () => {
  const props = {
    marks: ["jira", "confluence"],
    name: "Atlassian",
    sub: "Jira and Confluence",
    actionLabel: "Manage",
    onAction: noop,
  } as const;
  const view = html(<IntegrationCard {...props} state="on" badge="Active" by="Connected by Alex Kim" />);
  assert.match(view, /<li class="gx-igcard" data-state="on">/);
  assert.match(view, /<span class="gx-tag gx-tag--solid">Active<\/span>/);
  assert.match(view, /<h2 class="gx-igcard__name">Atlassian<\/h2><p class="gx-igcard__sub">Jira and Confluence<\/p>/);
  assert.match(view, /gx-btn--ghost gx-btn--sm">Manage<\/button><span class="gx-igcard__by">Connected by Alex Kim<\/span>/);
  assert.equal(view.match(/class="gx-igcard__logo"/g)?.length, 2);
  const utils = html(<IntegrationCard {...props} state="off" actionLabel="Set up" />);
  assert.match(utils, /data-state="off"/);
  assert.doesNotMatch(utils, /gx-tag|gx-igcard__by/);
  assert.match(utils, /class="gx-btn gx-btn--sm">Set up/);
  assert.match(
    html(<IntegrationCard {...props} state="stopped" badge="Reconnect required" />),
    /data-state="stopped".*<span class="gx-tag">Reconnect required/,
  );
  assert.match(html(<IntegrationCard {...props} state="unavailable" />), /data-state="unavailable".*disabled=""/);
});

test("IntegrationGrid and BuiltInList", () => {
  assert.equal(html(<IntegrationGrid label="Integrations">x</IntegrationGrid>), '<ul class="gx-iggrid" aria-label="Integrations">x</ul>');
  const view = html(<BuiltInList heading="Built in" items={[{ key: "mcp", mark: "mcp", title: "MCP", note: "Added by itself." }]} />);
  assert.match(view, /<section class="gx-builtin" aria-label="Built in"><p class="gx-eyebrow">Built in<\/p>/);
  assert.match(view, /<b>MCP<\/b><small>Added by itself\.<\/small>/);
});

test("PickList and PickRow: a fieldset of checkboxes", () => {
  const view = html(
    <PickList legend="Repositories">
      <PickRow label="acme/api" note="private" checked onChange={noop} />
      <PickRow label="acme/web" checked={false} disabled onChange={noop} />
    </PickList>,
  );
  assert.match(view, /<fieldset class="gx-pick"><legend class="gx-eyebrow">Repositories<\/legend>/);
  assert.match(view, /type="checkbox" checked=""\/><span>acme\/api<\/span><small>private<\/small>/);
  assert.match(view, /type="checkbox" disabled=""\/><span>acme\/web<\/span>/);
});

test("PickList: quiet tools above the rows, a scrolling box and live notes are opt-in", () => {
  const view = html(
    <PickList
      legend="Issues"
      scroll
      notes={["2 of 3 issues, and the epic"]}
      tools={[
        { label: "Select all", onClick: noop },
        { label: "Select none", disabled: true, onClick: noop },
      ]}
    >
      <PickRow label="NWO-1  One" checked onChange={noop} />
    </PickList>,
  );
  assert.match(view, /<div class="gx-pick__tools"><button type="button" class="gx-pick__tool">Select all<\/button>/);
  assert.match(view, /<button type="button" class="gx-pick__tool" disabled="">Select none<\/button>/);
  assert.match(view, /<div class="gx-pick__rows"><label class="gx-pick__row">/);
  assert.match(view, /<div class="gx-pick__notes" aria-live="polite"><p>2 of 3 issues, and the epic<\/p><\/div>/);
});

test("PickRow: text rows set the label in the body font, with a mono code and a quiet hint", () => {
  let view = html(<PickRow text code="NWO-1" label="One" note="Story" checked onChange={noop} />);
  assert.match(view, /<label class="gx-pick__row gx-pick__row--text">/);
  assert.match(view, /<span class="gx-pick__name"><code class="gx-pick__code">NWO-1<\/code> One<\/span><small>Story<\/small>/);
  view = html(<PickRow text label="Specs.pdf" hint="design/archive" checked onChange={noop} />);
  assert.match(view, /<span class="gx-pick__name">Specs\.pdf <span class="gx-pick__hint">design\/archive<\/span><\/span>/);
  view = html(<PickRow text label="Specs.pdf" checked onChange={noop} />);
  assert.match(view, /<span class="gx-pick__name">Specs\.pdf<\/span>/);
});

test("layout helpers", () => {
  assert.equal(html(<Page>x</Page>), '<main class="gx-page">x</main>');
  assert.equal(html(<Page busy>x</Page>), '<main class="gx-page" aria-busy="true">x</main>');
  assert.equal(html(<AppFrame>x</AppFrame>), '<div class="gx-frame">x</div>');
  assert.equal(html(<Stack>x</Stack>), '<div class="gx-stack">x</div>');
  assert.equal(html(<Stack onSubmit={noop}>x</Stack>), '<form class="gx-stack" noValidate="">x</form>');
  assert.equal(html(<Row>x</Row>), '<div class="gx-row">x</div>');
  assert.equal(html(<Split>x</Split>), '<div class="gx-split">x</div>');
  assert.equal(html(<DialogFoot>x</DialogFoot>), '<div class="gx-foot">x</div>');
  assert.equal(html(<Notice>x</Notice>), '<p class="gx-notice" role="status">x</p>');
  assert.match(html(<Notice role="alert">x</Notice>), /role="alert"/);
  assert.equal(html(<CodeLine>/specflow connect 1</CodeLine>), '<code class="gx-code">/specflow connect 1</code>');
  assert.equal(
    html(<SectionHead title="Collaborators" note="3 of 50" />),
    '<div class="gx-sectionhead"><b>Collaborators</b><span>3 of 50</span></div>',
  );
  assert.doesNotMatch(html(<SectionHead title="t" />), /<span>/);
});

test("ToolGrid: a labelled list, the name in mono then one sentence", () => {
  const view = html(<ToolGrid label="Tools" items={[{ name: "list_gaps", does: "Lists the open questions." }]} />);
  assert.equal(
    view,
    '<ul class="gx-tools" aria-label="Tools"><li><code class="gx-tools__name">list_gaps</code><span>Lists the open questions.</span></li></ul>',
  );
});

test("CountTable: a labelled table, the categories as row heads, a zero marked to show quieter", () => {
  const view = html(
    <CountTable label="Gaps" head="Category" columns={["Major", "Minor"]} rows={[{ key: "m", label: "Missing", counts: [4, 0] }]} />,
  );
  assert.equal(
    view,
    '<table class="gx-counts" aria-label="Gaps"><thead><tr><th scope="col">Category</th><th scope="col">Major</th><th scope="col">Minor</th></tr></thead>' +
      '<tbody><tr><th scope="row">Missing</th><td>4</td><td data-zero="">0</td></tr></tbody></table>',
  );
});

test("FileList: the word and the note only for a file that has them", () => {
  const view = html(
    <FileList
      label="Files"
      rows={[
        { key: "a", name: "spec.pdf", format: "PDF" },
        { key: "b", name: "x.png", format: "Images", tag: "Skipped", note: "Skipped: not imported." },
      ]}
    />,
  );
  assert.match(view, /^<ul class="gx-files" aria-label="Files">/);
  assert.match(view, /<li><span class="gx-files__name" title="spec\.pdf">spec\.pdf<\/span><span class="gx-files__meta">PDF<\/span><\/li>/);
  assert.match(view, /Images<span class="gx-tag">Skipped<\/span><\/span><small class="gx-files__why">Skipped: not imported\.<\/small>/);
});
