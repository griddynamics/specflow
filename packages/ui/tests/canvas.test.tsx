// The canvas components: structure, the state words, the colour rule (yellow only on a hub that carries a decision) and the reduced-motion
// block that stops every animation of canvas.css.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AddTile, Canvas, CanvasColumn, CanvasNode, CanvasZone, ConnectorCard, CopyRow, DropZone, Hub, StepList } from "../src/index";

const html = (node: ReactElement): string => renderToStaticMarkup(node);
const noop = (): undefined => undefined;
const css = readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "../src/styles/canvas.css"), "utf8");

test("Canvas: a labelled main with the wires under the zones; each wire carries its motion and two end caps", () => {
  const view = html(
    <Canvas
      label="Canvas"
      wires={{
        width: 100,
        height: 50,
        items: [
          {
            key: "a",
            d: "M0 0L10 10",
            motion: "flow",
            ends: [
              [0, 0],
              [10, 10],
            ],
          },
        ],
      }}
    >
      <CanvasZone id="graph" label="01 · Graph" live>
        <CanvasColumn variant="scroll">x</CanvasColumn>
      </CanvasZone>
    </Canvas>,
  );
  assert.match(view, /^<main class="gx-canvas" aria-label="Canvas"><svg class="gx-wires"[^>]*viewBox="0 0 100 50"/);
  assert.match(view, /<path class="gx-wire" data-testid="wire-a" data-motion="flow" d="M0 0L10 10"><\/path>/);
  assert.equal(view.split('class="gx-wire-end"').length - 1, 2);
  assert.match(view, /<section class="gx-zone" data-zone="graph" data-live="true" aria-label="01 · Graph">/);
  assert.match(view, /class="gx-col gx-col--scroll"/);
});

test("Hub: yellow only with a yellow decision; the decision is the text under it; a hub without a press is a group, with one a button", () => {
  const view = {
    yellow: html(
      <Hub
        id="graph"
        state="ready"
        tooltip="t"
        label="Graph"
        progress={null}
        decision={{ label: "Build graph", yellow: true }}
        onPress={noop}
      />,
    ),
    ink: html(<Hub id="plan" state="ready" tooltip="t" label="Plan" progress={null} decision={{ label: "Build plan", yellow: false }} />),
    plain: html(<Hub id="gap" state="on" tooltip="14 found" label="Gaps" progress={null} pill="3 critical" />),
  };
  assert.match(view.yellow, /data-yellow="true"/);
  assert.match(view.yellow, /<button type="button" class="gx-hub__tile" aria-label="Graph\. Build graph" title="t"/);
  assert.match(view.yellow, /<span class="gx-hub__decision" data-hole="true">Build graph<\/span>/);
  assert.match(view.ink, /data-yellow="false"/);
  assert.match(view.ink, /<span class="gx-hub__tile" role="group"/);
  assert.doesNotMatch(view.plain, /gx-hub__decision/);
  assert.match(view.plain, /gx-hub__pill[^>]*>3 critical</);
});

test("Hub: the ring fills with progress, or turns while the progress is not reported; no ring without progress", () => {
  const view = {
    unknown: html(<Hub id="graph" state="busy" tooltip="t" label="Graph" progress="unknown" />),
    half: html(<Hub id="plan" state="busy" tooltip="t" label="Plan" progress={0.5} />),
    none: html(<Hub id="plan" state="off" tooltip="t" label="Plan" progress={null} />),
  };
  assert.match(view.unknown, /gx-hub__ring" data-spin="true"/);
  assert.match(view.half, /data-spin="false"/);
  assert.match(view.half, /stroke-dashoffset="219\.9"/);
  assert.doesNotMatch(view.none, /gx-hub__ring/);
});

test("CanvasNode: the app mark, the name, one status line; a badge and a bar only when it has them; MCP carries its tag", () => {
  const view = {
    busy: html(
      <CanvasNode
        id="n1"
        mark="gdrive"
        name="Specs"
        meta="6 of 24 files"
        state="busy"
        badge="spin"
        progress={0.25}
        listing
        label="Google Drive Specs"
        onPress={noop}
      />,
    ),
    quiet: html(<CanvasNode id="n2" mark="mcp" name="MCP" meta="m" state="on" badge={null} progress={null} label="MCP" tag="Automatic" />),
    stopped: html(<CanvasNode id="n3" mark="gdrive" name="x" meta="m" state="stopped" badge="stop" progress={null} label="x" />),
    filled: html(<CanvasNode id="n4" mark="gdrive" name="x" meta="m" state="busy" badge="spin" progress={0.5} label="x" />),
  };
  assert.match(view.busy, /data-testid="canvas-node" data-state="busy" data-anchor="n1" data-kind="gdrive"/);
  assert.match(view.busy, /<button type="button" class="gx-cnode__tile" aria-label="Google Drive Specs"/);
  assert.match(view.busy, /data-badge="spin"/);
  assert.match(view.busy, /gx-cnode__bar" data-listing="true"/);
  assert.doesNotMatch(view.busy, /style=/);
  assert.match(view.filled, /<i style="width:50%">/);
  assert.doesNotMatch(view.quiet, /gx-cnode__badge|gx-cnode__bar|<button/);
  assert.match(view.quiet, /gx-cnode__tag">Automatic</);
  assert.match(view.stopped, /data-badge="stop"/);
});

test("AddTile, ConnectorCard, CopyRow, DropZone and StepList: the parts of the plus dialog", () => {
  assert.match(html(<AddTile big label="Add source" onPress={noop} />), /class="gx-add gx-add--big"[^>]*>.*Add source/);
  assert.doesNotMatch(html(<AddTile big={false} label="Add source" onPress={noop} />), /gx-add--big/);
  assert.match(html(<ConnectorCard mark="slack" name="Slack" line="l" tag="For Gap" disabled onPress={noop} />), /aria-disabled="true"/);
  assert.match(
    html(<ConnectorCard mark="slack" name="Slack" line="l" tag="Available" onPress={noop} />),
    /^<button type="button" class="gx-ccard"><span/,
  );
  assert.match(
    html(<CopyRow text="/mcp" copyLabel="Copy" copiedLabel="Copied" name="command" />),
    /<code[^>]*>\/mcp<\/code><button[^>]*aria-label="Copy command"/,
  );
  assert.match(
    html(<DropZone title="Drop" note="n" actionLabel="Choose files" accept=".pdf" onFiles={noop} />),
    /<input type="file" multiple="" hidden="" accept="\.pdf" aria-label="Choose files"/,
  );
  assert.match(
    html(
      <StepList start={2}>
        <li>a</li>
      </StepList>,
    ),
    /<ol class="gx-steps" start="2">/,
  );
});

test("canvas.css: every animation stops under reduced motion, a moving wire becomes solid with an arrow, and yellow is only the hub decision", () => {
  const block = css.slice(css.lastIndexOf("@media (prefers-reduced-motion: reduce)"));
  const animated = css
    .split("}")
    .filter((rule) => /\banimation:\s*(?!none)/.test(rule))
    .map((rule) => rule.slice(0, rule.indexOf("{")).trim());
  assert.ok(animated.length >= 5, String(animated.length));
  for (const selector of animated) {
    for (const part of selector.split(",")) assert.ok(block.includes(part.trim()), `no reduced-motion rule for ${part.trim()}`);
  }
  assert.match(block, /\.gx-wire\[data-motion="flow"\][\s\S]*stroke-dasharray: none;[\s\S]*marker-end: url\("#gx-wire-arrow"\)/);
  const yellowRules = css.split("}").filter((rule) => rule.includes("--gx-yellow"));
  assert.equal(yellowRules.length, 1);
  assert.match(yellowRules[0] ?? "", /\.gx-hub\[data-yellow="true"\] \.gx-hub__tile/);
});

test("a dense column packs four sources or more; the pill has a paper ring that keeps it apart from an ink hub; a command wraps instead of being cut off", () => {
  assert.match(
    html(
      <CanvasColumn variant="scroll" dense>
        x
      </CanvasColumn>,
    ),
    /class="gx-col gx-col--scroll gx-col--dense"/,
  );
  assert.doesNotMatch(html(<CanvasColumn variant="scroll">x</CanvasColumn>), /dense/);
  const pill = css.slice(css.indexOf(".gx-hub__pill {"), css.indexOf("}", css.indexOf(".gx-hub__pill {")));
  assert.match(pill, /box-shadow: 0 0 0 3px var\(--gx-paper\)/);
  const text = css.slice(css.indexOf(".gx-copyrow__text {"), css.indexOf("}", css.indexOf(".gx-copyrow__text {")));
  assert.match(text, /overflow-wrap: anywhere/);
  assert.doesNotMatch(text, /white-space: nowrap|overflow-x/);
});
