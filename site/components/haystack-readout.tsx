import type { JSX } from "react";
import type { Fact } from "../lib/haystack-data";
import type { Mode, SearchResult } from "../lib/haystack-draw";

const TOTAL_CELLS = 10;

export function HaystackLegend({ mode }: Readonly<{ mode: Mode }>): JSX.Element {
  return (
    <div className="hs-legend">
      {mode === "search" ? (
        <>
          <span>
            <i className="need" />
            needed fact, found
          </span>
          <span>
            <i className="miss" />
            needed fact, missed
          </span>
          <span>
            <i />
            passage in context
          </span>
          <span>
            <i className="dot" />
            other passage
          </span>
        </>
      ) : (
        <>
          <span>
            <i className="need" />
            needed fact
          </span>
          <span>
            <i className="hop" />
            item on the way
          </span>
          <span>
            <i className="line" />
            link in the graph
          </span>
          <span>
            <i className="dash" />
            reads alike, no link
          </span>
          <span>
            <i className="dot" />
            other passage, not needed
          </span>
        </>
      )}
    </div>
  );
}

/** What fills the agent's context, as cells: needed facts, lookalikes and room to spare. */
function contextCells(mode: Mode, res: SearchResult, count: number): string[] {
  if (mode === "map") return [...Array<string>(count).fill("need"), ...Array<string>(TOTAL_CELLS - count).fill("empty")];
  if (res.whole) return Array.from({ length: TOTAL_CELLS }, (_, i) => (i % 2 === 0 ? "need" : "look"));
  return res.top.map((t) => (t.k ? "need" : "look"));
}

function sayFor(mode: Mode, whole: boolean): string {
  if (mode === "map") return "Only the connected facts, with room to spare. The same facts at any project size.";
  if (whole) return "The whole project fits. Every needed fact is in context, next to everything else. At this size, search works.";
  return "Ten passages that read like the task. Most are lookalikes: other features, old notes, similar wording.";
}

function detail(mode: Mode, res: SearchResult, fact: Fact, ok: boolean): string {
  if (mode === "map") return `${fact.how} · ${fact.src}`;
  if (!ok) return fact.miss ?? "";
  return res.whole && !fact.hit ? "Found: the whole project fits in context" : (fact.hit ?? "");
}

export function HaystackReadout({ mode, res, needed }: Readonly<{ mode: Mode; res: SearchResult; needed: Fact[] }>): JSX.Element {
  return (
    <div className="hs-out" aria-live="polite">
      <p className="gx-eyebrow">Task: build Feature 2</p>
      <p className="h4">{mode === "search" ? "What agent search returns" : "What the agent reaches in the graph"}</p>
      <ul className="hs-list">
        {needed.map((fact) => {
          const ok = mode === "map" || res.found.has(fact.k);
          return (
            <li key={fact.k} className={ok ? "ok" : "miss"}>
              <span className="mk" aria-hidden="true" />
              <span>
                <b>{fact.name}</b>
                <small>
                  {detail(mode, res, fact, ok)}
                  {mode === "map" ? <span className="tool-name">tool: {fact.tool}</span> : null}
                </small>
              </span>
            </li>
          );
        })}
      </ul>
      <div className="stack-8">
        <p className="gx-eyebrow">What fills the agent&apos;s context</p>
        <div className="hs-ctx" aria-hidden="true">
          {contextCells(mode, res, needed.length).map((cell, i) => (
            <i key={i} className={cell} />
          ))}
        </div>
        <p className="small muted">{sayFor(mode, res.whole)}</p>
      </div>
    </div>
  );
}
